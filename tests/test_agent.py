"""A1 + A2: the agent runtime — tool call execution, parking and resuming, approvals, verbatim replay (cache), crash recovery."""

import json

import pytest
from test_studio import drain

from video_agent.daemon import Actions
from video_agent.planner import Planner
from video_agent.studio import Studio


def call(name, args=None, cid=None):
    return {"id": cid or f"c_{name}", "name": name, "arguments": json.dumps(args or {})}


class Script:
    """Returns preset LLM outputs in order; records the messages each call received, to check "extend only" and call IDs."""

    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def __call__(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


@pytest.fixture
def env(make_orch, fake):
    studio = Studio(make_orch(fake), round_size=2)
    said = []

    def make(*steps):
        script = Script(*steps)
        planner = Planner(studio.orch.cfg, studio.store, studio.orch.registry, chat=script)
        a = Actions(studio, planner)
        a.channel_send["web"] = lambda t, b=None: said.append((t, b))
        return a, script

    return studio, make, said


def run_all(a):
    while not a.jobs.empty():
        a.jobs.get()[0]()


def last_run(studio):
    return studio.store.runs(None)[-1]


def test_tool_loop_waits_for_render_then_continues(env):
    studio, make, said = env

    def queue(msgs):
        return {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": "girl"})]}

    def wait(msgs):
        rid = json.loads(msgs[-1]["content"])["round_id"]
        return {"tool_calls": [call("wait_for_round", {"round_id": rid})]}

    a, script = make(
        {"tool_calls": [call("get_status")]}, queue, wait, {"text": "Done: 2 candidates ready."}
    )
    a.inbound("web", "make 2 portraits", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    # rendering: the conversation pauses and holds no thread
    assert run["status"] == "waiting_event"
    drain(studio)  # the Worker finishes the render
    rnd = studio.store.rounds(state="review")[0]
    a.runtime.on_round_ready(rnd)  # the daemon's notify_round calls this
    run_all(a)
    assert last_run(studio)["status"] == "done"
    assert said[-1][0] == "Done: 2 candidates ready."
    roles = [m["role"] for m in studio.store.agent_msgs(run["id"])]
    assert roles == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    # every request is a verbatim extension of the previous one (prefix cache)
    for prev, nxt in zip(script.seen, script.seen[1:], strict=False):
        assert nxt[: len(prev)] == prev
    waited = json.loads(script.seen[-1][-1]["content"])
    assert waited["state"] == "review" and len(waited["candidates"]) == 2


def test_multiple_tool_calls_keep_order_and_ids(env):
    _studio, make, _ = env
    a, script = make(
        {
            "tool_calls": [
                call("get_status", cid="A"),
                call("nope", cid="B"),
                {"id": "C", "name": "get_round", "arguments": "{bad json"},
            ]
        },
        {"text": "ok"},
    )
    a.inbound("web", "check", lambda t, b=None: None)
    run_all(a)
    sent = script.seen[1]
    tool_msgs = [m for m in sent if m["role"] == "tool"]
    # every call has a result, order unchanged
    assert [m["tool_call_id"] for m in tool_msgs] == ["A", "B", "C"]
    assert "unknown tool" in tool_msgs[1]["content"] and "not valid JSON" in tool_msgs[2]["content"]
    assert sent[-4]["tool_calls"][2]["id"] == "C"


def test_parks_while_video_renders_and_resumes(env):
    studio, make, _ = env
    a, script = make({"text": "answer"})
    rm = studio.orch.resources
    rm.render_active = {
        "task_id": "x",
        "family": "freevideo-h3",
        "kind": "video",
        "exclusive": True,
    }
    a.inbound("web", "hello", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_resources" and script.seen == []
    rm.render_done(studio.orch.registry.template("h3-t2v"), released=True, unloaded_llm=False)
    run_all(a)
    assert last_run(studio)["status"] == "done" and len(script.seen) == 1


def test_selection_needs_approval_unless_named(env):
    studio, make, said = env
    rnd = studio.start_round("flux2-t2i", {"seed": 1})
    drain(studio)
    a, _ = make(
        {
            "tool_calls": [
                call("propose_selection", {"round_id": rnd["id"], "idx": 2, "role": "character"})
            ]
        },
        {"text": "locked"},
    )
    a.inbound("web", "pick the best one", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_user"
    assert studio.store.get_round(rnd["id"])["state"] == "review"  # not picked yet
    approve = next(b for _t, b in said if b)[0][0][1]
    a.button("web", approve, lambda t, b=None: None)
    run_all(a)
    assert studio.store.get_round(rnd["id"])["decision"]["idx"] == 2
    assert "character" in studio.store.pack(rnd["project_id"])["assets"]
    assert last_run(studio)["status"] == "done"

    rnd2 = studio.start_round("flux2-t2i", {"seed": 5})
    drain(studio)
    a2, _ = make(
        {"tool_calls": [call("propose_selection", {"round_id": rnd2["id"], "idx": 1})]},
        {"text": "done"},
    )
    a2.inbound("web", "就用 #1", lambda t, b=None: None)  # named in your words → executed directly
    run_all(a2)
    assert studio.store.get_round(rnd2["id"])["decision"]["idx"] == 1


def test_user_choosing_via_review_buttons_answers_pending_approval(env):
    studio, make, _ = env
    rnd = studio.start_round("flux2-t2i", {"seed": 1})
    drain(studio)
    a, script = make(
        {"tool_calls": [call("propose_selection", {"round_id": rnd["id"], "idx": 2})]},
        {"text": "noted"},
    )
    a.inbound("web", "pick one", lambda t, b=None: None)
    run_all(a)
    studio.select(rnd["id"], 1)  # you picked #1 directly on the review page
    run_all(a)
    result = json.loads(script.seen[-1][-1]["content"])
    assert result["user_decision"] == "select" and result["selected"] == 1


def test_ask_user_and_recovery_after_restart(env):
    studio, make, _ = env
    a, script = make(
        {"tool_calls": [call("ask_user", {"question": "Image or video?"})]}, {"text": "video it is"}
    )
    a.inbound("web", "make something", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_user"
    a.inbound("web", "video", lambda t, b=None: None)  # answers the question, no new run
    run_all(a)
    assert len(studio.store.runs(None)) == 1 and last_run(studio)["status"] == "done"
    assert json.loads(script.seen[-1][-1]["content"]) == {"answer": "video"}

    # crash recovery: the round being waited for finished while the daemon was down
    rnd = studio.start_round("flux2-t2i", {"seed": 9}, n=1)
    b, _ = make({"tool_calls": [call("wait_for_round", {"round_id": rnd["id"]})]}, {"text": "back"})
    b.inbound("web", "wait for it", lambda t, b=None: None)
    run_all(b)
    drain(studio)
    c, script_c = make({"text": "back after restart"})  # new process
    c.runtime.recover()
    run_all(c)
    assert last_run(studio)["status"] == "done" and len(script_c.seen) == 1


def test_step_budget(env):
    studio, make, said = env
    studio.orch.cfg.agent_max_steps = 2
    a, _ = make(
        {"tool_calls": [call("get_status", cid="1")]},
        {"tool_calls": [call("get_status", cid="2")]},
        {"text": "never"},
    )
    a.runtime.max_steps = 2
    a.inbound("web", "loop", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "done" and "budget" in said[-1][0]


# ---------------- policy layer (P1 / P2 / D1) ----------------
def test_execution_policy_rejects_without_changing_args(env):
    studio, make, _ = env
    a, script = make(
        {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": "x", "n": 20})]},
        {"text": "ok"},
    )
    a.inbound("web", "lots", lambda t, b=None: None)
    run_all(a)
    err = json.loads(script.seen[-1][-1]["content"])["error"]
    assert "exceeds the policy limit of 8" in err
    assert studio.store.rounds(state="generating") == []  # not quietly changed to 8 images


def test_user_override_policy_file(env, tmp_path):
    studio, make, _ = env
    (studio.orch.cfg.data_dir / "policies.yaml").write_text(
        "execution:\n  tools:\n    queue_round: {allow: false}\n"
        "    propose_selection: {allow: true, approval: always}\n"
    )
    a, script = make(
        {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": "x"})]},
        {"text": "ok"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    assert "not permitted" in json.loads(script.seen[-1][-1]["content"])["error"]
    rnd = studio.start_round("flux2-t2i", {"seed": 1})
    drain(studio)
    b, _ = make(
        {"tool_calls": [call("propose_selection", {"round_id": rnd["id"], "idx": 1})]},
        {"text": "ok"},
    )
    b.inbound("web", "就用 #1", lambda t, b=None: None)  # policy always: buttons even when named
    run_all(b)
    assert studio.store.get_round(rnd["id"])["state"] == "review"


def test_oom_retries_once_with_same_seeds_then_returns_to_llm(make_orch):
    from conftest import FakeComfy

    fake = FakeComfy(fail=True)  # the executor reports "OOM"
    studio = Studio(make_orch(fake), round_size=1)
    script = Script(
        {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": "x", "n": 1})]},
        lambda m: {
            "tool_calls": [
                call("wait_for_round", {"round_id": json.loads(m[-1]["content"])["round_id"]})
            ]
        },
        {"text": "it failed twice"},
    )
    planner = Planner(studio.orch.cfg, studio.store, studio.orch.registry, chat=script)
    a = Actions(studio, planner)
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    first = studio.store.rounds(state="generating")[0]
    seed = studio.store.round_tasks(first["id"])[0]["params"]["seed"]
    drain(studio)
    a.runtime.on_round_ready(studio.store.get_round(first["id"]))
    # policy: retry once with the same parameters and seed, without the LLM
    retry = studio.store.rounds(state="generating")[0]
    assert retry["parent_round"] == first["id"] and len(script.seen) == 2
    assert studio.store.round_tasks(retry["id"])[0]["params"]["seed"] == seed
    drain(studio)
    a.runtime.on_round_ready(studio.store.get_round(retry["id"]))
    run_all(a)
    assert json.loads(script.seen[-1][-1]["content"])["outcome"] == "failed:oom"
    assert last_run(studio)["status"] == "done"


def test_pipeline_runs_steps_without_waking_llm(env):
    studio, make, _ = env
    steps = [
        {"template": "flux2-t2i", "prompt": "portrait", "n": 1},
        {"template": "h3-i2v", "prompt": "she turns", "image": "@prev"},
    ]
    a, script = make(
        {"tool_calls": [call("queue_pipeline", {"steps": steps})]}, {"text": "pipeline done"}
    )
    a.inbound("web", "portrait then video", lambda t, b=None: None)
    run_all(a)
    drain(studio)
    first = studio.store.rounds(state="review")[0]
    a.runtime.on_round_ready(first)  # step 1 done → policy auto_next
    video = studio.store.rounds(state="generating")[0]
    assert video["template"] == "h3-i2v" and video["parent_round"] == first["id"]
    img_task = studio.store.round_tasks(first["id"])[0]
    assert studio.store.round_tasks(video["id"])[0]["params"]["image"].endswith(
        img_task["outputs"][0]["file"]
    )
    assert len(script.seen) == 1  # the LLM was not woken in between
    drain(studio)
    a.runtime.on_round_ready(studio.store.get_round(video["id"]))
    run_all(a)
    result = json.loads(script.seen[-1][-1]["content"])
    assert result["finished"] and len(result["pipeline"]) == 2
    assert last_run(studio)["status"] == "done"


def test_finished_runs_are_compact_and_stable_across_runs(env):
    _studio, make, _ = env
    a, script = make(
        {"tool_calls": [call("get_status")]},
        {"text": "first answer", "reasoning": "x"},
        {"text": "second answer"},
        {"text": "third answer"},
    )
    for text in ("one", "two", "three"):
        a.inbound("web", text, lambda t, b=None: None)
        run_all(a)
    two, three = script.seen[2], script.seen[3]
    # run 1 is folded into [your words, final reply], without the tool exchange
    assert two[1] == {"role": "user", "content": "## User\none"}
    assert two[2] == {"role": "assistant", "content": "first answer"}
    # in every later request this part is verbatim the same (cache hits)
    assert three[:3] == two[:3]


def test_queue_and_runs_api(env):
    from fastapi.testclient import TestClient

    from video_agent.web import create_app

    studio, make, _ = env
    a, _ = make({"tool_calls": [call("ask_user", {"question": "Which style?"})]})
    a.inbound("web", "make something", lambda t, b=None: None)
    run_all(a)
    studio.start_round("flux2-t2i", {"seed": 3})
    studio.orch.resources.record()
    c = TestClient(create_app(studio.orch.cfg, a))
    q = c.get("/api/queue").json()
    assert len(q["plan"]) == 2 and q["plan"][0]["end"] > q["plan"][0]["start"]
    assert q["runs"][0]["status"] == "waiting_user" and len(q["history"]) == 1
    sid = studio.current_session()["id"]
    run = c.get(f"/api/runs?session={sid}").json()[-1]
    assert c.post(f"/api/runs/{run['id']}/cancel").json()["ok"]
    assert studio.store.get_run(run["id"])["status"] == "cancelled"


def test_cancelled_round_releases_waiting_run(env):
    studio, make, _ = env
    a, script = make(
        {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": "x", "n": 1})]},
        lambda m: {
            "tool_calls": [
                call("wait_for_round", {"round_id": json.loads(m[-1]["content"])["round_id"]})
            ]
        },
        {"text": "it was cancelled"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    rnd = studio.store.rounds(state="generating")[0]
    studio.cancel_round(rnd["id"], "user cancelled")
    run_all(a)
    assert last_run(studio)["status"] == "done"
    assert json.loads(script.seen[-1][-1]["content"])["decision"]["action"] == "cancel"


def test_parked_message_tells_you_once_and_resumes_when_film_ends(env):
    """A Telegram message arrives while a film is shooting: tell you it is queued (only once), continue automatically when shooting ends."""
    studio, make, said = env
    a, script = make({"text": "answer"})
    rm = studio.orch.resources
    rm.film_started("f1")
    a.inbound("web", "进度如何", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_resources" and script.seen == []
    parked = [t for t, _ in said if "FreeVideo" in t]
    assert len(parked) == 1
    # other renders ending also wake it: still shooting → keep waiting, no repeated notice
    a.runtime.on_resources_freed()
    run_all(a)
    assert len([t for t, _ in said if "FreeVideo" in t]) == 1 and script.seen == []
    status = a.status_text()
    assert "1" in status.split("Agent")[1] and "Film shooting: 1" in status
    rm.films_shooting(set())  # shooting done → wake the waiters
    run_all(a)
    assert last_run(studio)["status"] == "done" and len(script.seen) == 1
    texts = [t for t, _ in said]
    assert texts[-2].startswith("Memory is free") and texts[-1] == "answer"


def test_run_stage_suspends_the_agent_until_the_stage_stops_running(env, tmp_path):
    """The agent queued a running stage again and again while an adaptation took 12 minutes;
    now it is suspended on the stage and resumed with its result."""
    from tests.test_production import FakeLLM, run_skills

    from video_agent.config import ROOT
    from video_agent.production import Production, load_workflows
    from video_agent.skills import SkillLibrary

    studio, make, said = env
    stage_llm = FakeLLM()
    stage_llm.park = True  # the LLM is busy: adaptation stays queued for now
    prod = Production(
        studio,
        SkillLibrary((ROOT / "skills", "builtin")),
        load_workflows(ROOT / "workflows"),
        stage_llm,
        log=lambda m: None,
        work_dir=tmp_path / "productions",
    )
    pr = prod.create("novel-to-film", "x", {"source": "Mei lost her cat"})
    stage_llm.park = False
    run_skills(prod)  # brief + adaptation → review
    prod.review(pr["id"], "adaptation", "redo", note="again")  # adaptation running again

    args = {"production_id": pr["id"], "stage": "adaptation"}
    a, script = make(
        {"tool_calls": [call("run_stage", args, cid="1")]},
        {"tool_calls": [call("run_stage", args, cid="2")]},
        {"text": "adaptation ready for your review"},
    )
    a.runtime.production = prod
    prod.listeners.append(a.runtime.on_stage_event)
    n_before = len(prod.store.artifacts(pr["id"], "adaptation"))
    a.inbound("web", "go on", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert run["status"] == "waiting_event"  # suspended, not polling
    assert len(prod.store.artifacts(pr["id"], "adaptation")) == n_before  # nothing re-queued

    run_skills(prod)  # the adaptation finishes → the agent resumes with the result
    run_all(a)
    first = json.loads(next(m for m in script.seen[1] if m.get("tool_call_id") == "1")["content"])
    assert first["stage"] == "adaptation" and first["state"] == "review"
    assert first["next_action"]["type"] == "review"
    # the second call found it no longer running and returned at once
    run_all(a)
    assert (
        last_run(studio)["status"] == "done" and said[-1][0] == "adaptation ready for your review"
    )


def test_create_and_review_suspend_until_the_production_needs_the_user(env, tmp_path):
    from tests.test_production import FakeLLM, run_skills

    from video_agent.config import ROOT
    from video_agent.production import Production, load_workflows
    from video_agent.skills import SkillLibrary

    studio, make, said = env
    prod = Production(
        studio,
        SkillLibrary((ROOT / "skills", "builtin")),
        load_workflows(ROOT / "workflows"),
        FakeLLM(),
        log=lambda m: None,
        work_dir=tmp_path / "productions",
    )

    def after_create(messages):
        res = json.loads(messages[-1]["content"])
        return {
            "tool_calls": [
                call(
                    "review_stage",
                    {
                        "production_id": res["production_id"],
                        "stage": "adaptation",
                        "action": "approve",
                    },
                    cid="2",
                )
            ]
        }

    a, script = make(
        {
            "tool_calls": [
                call(
                    "create_production",
                    {"workflow": "novel-to-film", "title": "x", "source": "Mei lost her cat"},
                    cid="1",
                )
            ]
        },
        after_create,
        {"text": "bible ready"},
    )
    a.runtime.production = prod
    prod.listeners.append(a.runtime.on_stage_event)
    a.inbound("web", "make a film", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_event"  # brief + adaptation still to run

    run_skills(prod)  # adaptation → review: the agent resumes and approves
    run_all(a)
    created = json.loads(next(m for m in script.seen[1] if m.get("tool_call_id") == "1")["content"])
    assert created["next_action"] == {"type": "review", "stage": "adaptation"}
    assert last_run(studio)["status"] == "waiting_event"  # the bible is running now

    run_skills(prod)  # bible → review: resumed again
    run_all(a)
    reviewed = json.loads(
        next(m for m in script.seen[2] if m.get("tool_call_id") == "2")["content"]
    )
    assert reviewed["artifact"]["status"] == "current"
    assert reviewed["next_action"] == {"type": "review", "stage": "bible"}
    assert last_run(studio)["status"] == "done" and said[-1][0] == "bible ready"
