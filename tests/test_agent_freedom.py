"""Agent mode: your messages steer a working run, renders the watch gave up on are reported as still
running (and can be stopped), the planner queue serves you first, and saved ComfyUI workflows are
converted to API graphs the way the ComfyUI frontend does."""

import json

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — env is a fixture

from video_agent.comfy_workflows import (
    Catalog,
    WorkflowError,
    apply_overrides,
    canvas_to_api,
    summary,
)
from video_agent.daemon import JobQueue


def _queue_and_wait(prompt="girl"):
    def queue(msgs):
        return {"tool_calls": [call("queue_round", {"template": "flux2-t2i", "prompt": prompt})]}

    def wait(msgs):
        rid = json.loads(msgs[-1]["content"])["round_id"]
        return {"tool_calls": [call("wait_for_round", {"round_id": rid}, cid="w1")]}

    return queue, wait


def _to_timeout(store, task_id):
    for state in ("validated", "waiting_memory", "submitted", "running"):
        store.transition(task_id, state)
    store.update(task_id, prompt_id=f"job-{task_id}")
    store.transition(task_id, "timeout", error="monitor timeout")


def test_your_message_steers_the_run_waiting_for_a_render(env):  # noqa: F811
    studio, make, _said = env
    queue, wait = _queue_and_wait()
    a, script = make(queue, wait, {"text": "switching to blue"})
    a.inbound("web", "make 2 portraits", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert run["status"] == "waiting_event"
    replies = []
    a.inbound("web", "actually make her hair blue", lambda t, b=None: replies.append(t))
    assert "already working on" in replies[-1]
    run_all(a)
    assert len(studio.store.runs(None)) == 1  # no second run: the same one continues
    sent = script.seen[-1]
    waited = json.loads(sent[-2]["content"])
    assert waited["interrupted"] and waited["waiting_for"].startswith("round:")
    assert waited["round"]["state"] == "generating"  # the render itself keeps going
    assert sent[-1]["role"] == "user" and "actually make her hair blue" in sent[-1]["content"]
    assert last_run(studio)["status"] == "done"


def test_new_request_prefix_starts_a_separate_run(env):  # noqa: F811
    studio, make, _ = env
    queue, wait = _queue_and_wait()
    a, _ = make(queue, wait, {"text": "other task done"})
    a.inbound("web", "make 2 portraits", lambda t, b=None: None)
    run_all(a)
    a.inbound("web", "new request: what is the weather", lambda t, b=None: None)
    run_all(a)
    assert len(studio.store.runs(None)) == 2
    assert studio.store.runs(None)[0]["status"] == "waiting_event"


def test_message_while_the_llm_answers_is_not_lost(env):  # noqa: F811
    studio, make, said = env
    a = None

    def final(msgs):
        # you write while the model is producing its final answer
        a.inbound("web", "one more thing: add a hat", lambda t, b=None: None)
        return {"text": "here you go"}

    a, script = make({"tool_calls": [call("get_status")]}, final, {"text": "added the hat"})
    a.inbound("web", "status please", lambda t, b=None: None)
    run_all(a)
    assert said[-1][0] == "added the hat"
    assert "add a hat" in script.seen[-1][-1]["content"]
    assert last_run(studio)["status"] == "done"


def test_a_render_whose_watch_timed_out_is_reported_still_running(env):  # noqa: F811
    studio, make, _ = env
    queue, wait = _queue_and_wait()
    a, script = make(
        queue,
        wait,
        lambda msgs: {
            "tool_calls": [
                call(
                    "wait_for_round",
                    {"round_id": json.loads(msgs[-1]["content"])["round_id"]},
                    cid="w2",
                )
            ]
        },
        {"text": "it finished"},
    )
    a.inbound("web", "make 2 portraits", lambda t, b=None: None)
    run_all(a)
    rnd = studio.store.rounds(state="generating")[0]
    tasks = studio.store.round_tasks(rnd["id"])
    for t in tasks:
        _to_timeout(studio.store, t["id"])
    rnd, ready = studio.refresh_round(rnd["id"])
    assert ready
    a.runtime.on_round_ready(rnd)
    run_all(a)
    got = json.loads(script.seen[2][-1]["content"])
    assert got["outcome"] == "still_running" and "wait_for_round" in got["hint"]
    # waiting again parks the run instead of returning "review" at once
    assert last_run(studio)["status"] == "waiting_event"
    # the late result lands (the Worker's reconcile): the agent is woken with it
    for t in tasks:
        studio.store.transition(t["id"], "review", outputs=[{"file": "x.png", "kind": "image"}])
    a.runtime.on_round_ready(studio.store.get_round(rnd["id"]))
    run_all(a)
    assert json.loads(script.seen[-1][-1]["content"])["outcome"] == "succeeded"
    assert last_run(studio)["status"] == "done"


def test_stop_round_cancels_a_render_the_watch_gave_up_on(env):  # noqa: F811
    studio, _make, _ = env
    rnd = studio.start_round("flux2-t2i", {"prompt": "x"}, 1, None, "t", origin="web")
    t = studio.store.round_tasks(rnd["id"])[0]
    _to_timeout(studio.store, t["id"])
    cancelled = []
    studio.cancel_job = lambda task: cancelled.append(task["prompt_id"]) or True
    out = studio.stop_round(rnd["id"])
    assert cancelled == [f"job-{t['id']}"] and out["interrupted"] == 1
    assert studio.store.get(t["id"])["state"] == "failed"


def test_job_queue_serves_you_before_agent_continuations():
    q = JobQueue()
    q.put(("agent-1",), JobQueue.AGENT)
    q.put(("prewarm",), JobQueue.BACKGROUND)
    q.put(("agent-2",), JobQueue.AGENT)
    q.put(("you",), JobQueue.USER)
    assert [q.get()[0] for _ in range(4)] == ["you", "agent-1", "agent-2", "prewarm"]
    assert q.empty()


# ---- saved ComfyUI workflows ----
OBJECT_INFO = {
    "Loader": {
        "input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
    },
    "Noise": {
        "input": {"required": {"noise_seed": ["INT", {"control_after_generate": True}]}},
        "input_order": {"required": ["noise_seed"]},
    },
    "LoadImage": {
        "input": {"required": {"image": [["p.png"], {"image_upload": True}], "channel": [["red"]]}},
        "input_order": {"required": ["image", "channel"]},
    },
    "Gen": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "prompt": ["STRING", {"multiline": True}],
                "length": ["INT", {}],
                "steps": ["INT", {}],
            },
            "optional": {"ref": ["IMAGE"], "preview": ["RES_PREVIEW"]},
        },
        "input_order": {
            "required": ["model", "prompt", "length", "steps"],
            "optional": ["ref", "preview"],
        },
    },
    "Lora": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "lora_name": [["l.safetensors"]],
                "strength_model": ["FLOAT", {}],
            }
        },
        "input_order": {"required": ["model", "lora_name", "strength_model"]},
    },
}


def canvas():
    return {
        "nodes": [
            {"id": 1, "type": "MarkdownNote", "widgets_values": ["## Draft first\n124 frames"]},
            {"id": 2, "type": "Loader", "widgets_values": ["a.safetensors"], "mode": 0},
            {
                "id": 3,
                "type": "Lora",
                "mode": 4,  # bypassed: model passes straight through
                "inputs": [{"name": "model", "type": "MODEL", "link": 10}],
                "outputs": [{"type": "MODEL"}],
                "widgets_values": ["l.safetensors", 0.8],
            },
            {"id": 4, "type": "Reroute", "inputs": [{"name": "", "type": "*", "link": 11}]},
            {"id": 5, "type": "PrimitiveNode", "widgets_values": ["a long prompt"]},
            {"id": 6, "type": "Noise", "widgets_values": [42, "fixed"]},
            {"id": 7, "type": "LoadImage", "widgets_values": ["p.png", "red", "image"]},
            {
                "id": 8,
                "type": "Gen",
                "inputs": [
                    {"name": "model", "type": "MODEL", "link": 12},
                    {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": 13},
                    {"name": "length", "type": "INT", "widget": {"name": "length"}, "link": None},
                    {"name": "ref", "type": "IMAGE", "link": 14},
                    {"name": "ref_images.ref_image_0", "type": "IMAGE", "link": 15},
                ],
                "widgets_values": ["ignored (linked)", 250, 20],
            },
        ],
        "links": [
            [10, 2, 0, 3, 0, "MODEL"],
            [11, 3, 0, 4, 0, "MODEL"],
            [12, 4, 0, 8, 0, "MODEL"],
            [13, 5, 0, 8, 1, "STRING"],
            [14, 7, 0, 8, 3, "IMAGE"],
            [15, 7, 0, 8, 4, "IMAGE"],
        ],
    }


def test_canvas_to_api_resolves_frontend_only_parts():
    g = canvas_to_api(canvas(), OBJECT_INFO)
    assert set(g) == {"2", "6", "7", "8"}  # note, bypassed LoRA, reroute, primitive are gone
    assert g["6"]["inputs"] == {"noise_seed": 42}  # "fixed" is a frontend widget
    assert g["7"]["inputs"] == {"image": "p.png", "channel": "red"}  # upload value ignored
    gen = g["8"]["inputs"]
    assert gen["model"] == ["2", 0]  # through the reroute and the bypassed LoRA
    assert gen["prompt"] == "a long prompt"  # from the primitive node
    assert gen["length"] == 250 and gen["steps"] == 20
    assert gen["ref"] == ["7", 0] and gen["ref_images.ref_image_0"] == ["7", 0]
    assert "preview" not in gen


def test_overrides_and_summary():
    g = apply_overrides(canvas_to_api(canvas(), OBJECT_INFO), {"8.length": 124, "steps": 4})
    assert g["8"]["inputs"]["length"] == 124 and g["8"]["inputs"]["steps"] == 4
    with pytest.raises(WorkflowError):
        apply_overrides(g, {"8.model": "x"})  # a link, not a value
    with pytest.raises(WorkflowError):
        apply_overrides(g, {"nope": 1})
    s = summary(g)
    assert "2.ckpt_name=a.safetensors" in s["models"] and s["settings"]["8.length"] == 124


def test_unknown_node_type_is_reported():
    wf = {"nodes": [{"id": 1, "type": "Missing", "widgets_values": []}], "links": []}
    with pytest.raises(WorkflowError, match="no node type Missing"):
        canvas_to_api(wf, OBJECT_INFO)


def test_catalog_lists_folder_with_notes_and_hidden(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "Draft.json").write_text(json.dumps(canvas()))
    (flows / "Old.json").write_text(json.dumps(canvas()))
    (tmp_path / "comfy_workflows.yaml").write_text(
        f"dirs: [{flows}]\nworkflows:\n  Old: {{hidden: true}}\n  Draft: {{verified: true}}\n"
    )
    cat = Catalog(tmp_path)
    rows = cat.list(OBJECT_INFO)
    assert [r["name"] for r in rows] == ["Draft"]
    assert rows[0]["verified"] and rows[0]["notes"][0].startswith("## Draft first")
    assert rows[0]["settings"]["8.length"] == 250
    assert "Draft ✓verified: Draft first" in cat.index()
    assert cat.api("Draft", OBJECT_INFO, {"8.length": 124})["8"]["inputs"]["length"] == 124
