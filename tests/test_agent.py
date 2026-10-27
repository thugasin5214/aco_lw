"""A1 + A2：agent 运行时——tool call 执行、暂存与恢复、审批、逐字复现（缓存）、崩溃恢复。"""

import json

import pytest
from test_studio import drain

from video_agent.daemon import Actions
from video_agent.planner import Planner
from video_agent.studio import Studio


def call(name, args=None, cid=None):
    return {"id": cid or f"c_{name}", "name": name, "arguments": json.dumps(args or {})}


class Script:
    """按顺序返回预设的 LLM 输出；每次都记录收到的消息，用来检查“只延长”与 call ID。"""

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
    assert run["status"] == "waiting_event"  # 渲染中：对话暂停，不占线程
    drain(studio)  # Worker 跑完渲染
    rnd = studio.store.rounds(state="review")[0]
    a.runtime.on_round_ready(rnd)  # daemon 的 notify_round 会调用它
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
    # 每次请求都是上一次的逐字延长（前缀缓存）
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
    assert [m["tool_call_id"] for m in tool_msgs] == ["A", "B", "C"]  # 每个调用都有结果，顺序不变
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
    assert studio.store.get_round(rnd["id"])["state"] == "review"  # 还没选
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
    a2.inbound("web", "就用 #1", lambda t, b=None: None)  # 原话点名 → 直接执行
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
    studio.select(rnd["id"], 1)  # 你直接在审阅页选了 #1
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
    a.inbound("web", "video", lambda t, b=None: None)  # 回答提问，不开新运行
    run_all(a)
    assert len(studio.store.runs(None)) == 1 and last_run(studio)["status"] == "done"
    assert json.loads(script.seen[-1][-1]["content"]) == {"answer": "video"}

    # 崩溃恢复：等待中的轮在 daemon 停机期间完成了
    rnd = studio.start_round("flux2-t2i", {"seed": 9}, n=1)
    b, _ = make({"tool_calls": [call("wait_for_round", {"round_id": rnd["id"]})]}, {"text": "back"})
    b.inbound("web", "wait for it", lambda t, b=None: None)
    run_all(b)
    drain(studio)
    c, script_c = make({"text": "back after restart"})  # 新进程
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
