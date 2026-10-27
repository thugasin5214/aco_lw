"""R1 + Q1：资源把关、学习的估计、排队策略。"""

import threading
import time

import pytest

from video_agent import llm


class Ex:
    name = "fake"

    def __init__(self):
        self.released = 0
        self.is_busy = False

    def busy(self):
        return self.is_busy

    def release(self):
        self.released += 1
        return True


@pytest.fixture
def rm(make_orch, fake, monkeypatch):
    o = make_orch(fake)
    state = {"loaded": True, "unloads": 0}
    monkeypatch.setattr(llm, "loaded", lambda m: {"id": m} if state["loaded"] else None)

    def unload(m):
        state["unloads"] += 1
        state["loaded"] = False

    monkeypatch.setattr(llm, "unload", unload)
    o.resources.state = state
    o.resources.sleep = lambda s: None
    return o.resources


def test_llm_gate_during_video_vs_image(rm):
    reg = rm.registry
    assert rm.llm_admit()[0]
    gate = rm.prepare_render(reg.template("h3-t2v"), "smoke", Ex(), "t1")
    assert gate.ok and gate.unloaded_llm  # 视频独占：卸载 LLM
    ok, why = rm.llm_admit()
    assert not ok and "video" in why  # 视频渲染中，LLM 不能加载
    called = []
    rm.listeners.append(lambda: called.append(1))
    rm.render_done(reg.template("h3-t2v"), released=True, unloaded_llm=False)
    assert rm.llm_admit()[0] and called == [1]  # 渲染结束 → 唤醒等待者
    rm.state["loaded"] = True
    gate = rm.prepare_render(reg.template("flux2-t2i"), "standard", Ex(), "t2")
    assert gate.ok and not gate.unloaded_llm and rm.llm_admit()[0]  # 图片可与 LLM 共存


def test_render_waits_for_answer_in_progress(rm):
    """LLM 正在写回答时到来的视频渲染：等回答写完再卸载，绝不打断。"""
    order = []

    def answering():
        with rm.llm.inference():
            time.sleep(0.3)
            order.append("answer finished")

    th = threading.Thread(target=answering)
    th.start()
    time.sleep(0.05)
    rm.prepare_render(rm.registry.template("h3-t2v"), "smoke", Ex(), "t1")
    order.append(f"unloads={rm.state['unloads']}")
    th.join()
    assert order == ["answer finished", "unloads=1"]


def test_family_switch_releases_previous(rm):
    ex = Ex()
    reg = rm.registry
    rm.prepare_render(reg.template("flux2-t2i"), "standard", ex, "a")
    rm.render_done(reg.template("flux2-t2i"), released=False, unloaded_llm=False)
    assert rm.resident_family == "flux2-klein-4b-q8"
    gate = rm.prepare_render(reg.template("h3-t2v"), "smoke", ex, "b")
    assert ex.released == 1 and any("switching" in a for a in gate.actions)


def test_choose_next_priority_family_and_aging(rm):
    now = time.time()
    d = lambda i, tpl, pr, age=0: {
        "id": i,
        "template": tpl,
        "priority": pr,
        "created": now - age,
        "idx": 1,
        "tier": "standard",
    }
    rm.resident_family = "flux2-klein-4b-q8"
    drafts = [d("bg", "flux2-t2i", 0), d("you", "h3-t2v", 2), d("agent", "flux2-t2i", 1)]
    assert rm.choose_next(drafts)["id"] == "you"  # 你的请求优先
    drafts = [d("img", "flux2-t2i", 1), d("vid", "h3-t2v", 1)]
    assert rm.choose_next(drafts)["id"] == "img"  # 同族优先，省一次切换
    drafts = [d("new", "flux2-t2i", 1), d("old-bg", "flux2-t2i", 0, age=3600)]
    assert rm.choose_next(drafts)["id"] == "old-bg"  # 等太久的后台任务不会饿死


def test_estimates_learned_from_history(rm):
    t = rm.registry.template("flux2-t2i")
    assert rm.estimator.estimate(t, "standard").source == "default"
    o = rm.store
    task = o.create(
        template="flux2-t2i",
        template_sha="x",
        kind="image",
        tier="standard",
        params={},
        params_hash="h",
    )
    o.update(task["id"], metrics={"exec_s": 12.0})
    o.transition(task["id"], "validated")
    o.transition(task["id"], "waiting_memory")
    o.transition(task["id"], "submitted")
    o.transition(task["id"], "review")
    e = rm.estimator.estimate(t, "standard")
    assert e.exec_s == 12.0 and e.source == "history n=1"
    e = rm.estimator.estimate(t, "preview")  # 896×512 vs 1280×720 按成本比例外推
    assert e.source == "scaled from standard" and 5 < e.exec_s < 7


def test_plan_gives_eta_with_switch_cost(rm):
    now = time.time()
    drafts = [
        {
            "id": "a",
            "template": "flux2-t2i",
            "priority": 1,
            "created": now,
            "idx": 1,
            "tier": "standard",
        },
        {"id": "b", "template": "h3-t2v", "priority": 1, "created": now, "idx": 1, "tier": "smoke"},
    ]
    rm.resident_family = "flux2-klein-4b-q8"
    plan = rm.plan(drafts)
    assert [p["task_id"] for p in plan] == ["a", "b"]
    assert plan[1]["start"] - plan[0]["end"] == pytest.approx(15.0)  # 换族代价
    assert rm.resident_family == "flux2-klein-4b-q8"  # 模拟不改共享状态


def test_cross_process_gate_sees_other_peoples_video(rm, fake):
    """别的进程 / 你在 ComfyUI 网页手动提交的视频也要挡住 LLM 加载（跨进程把关）。"""
    from video_agent.backends import LMStudioBackend
    from video_agent.planner import Planner, PlannerUnavailable

    video_graph = {"3": {"class_type": "FreeVideoGenerate", "inputs": {}}}
    fake.prompts["someone-else"] = video_graph  # 不是本系统提交的
    rm.state["loaded"] = False
    ok, why = rm.llm_admit()
    assert not ok and "video render" in why
    rm.state["loaded"] = True
    rm._ext_cache = None
    assert rm.llm_admit()[0]  # 已加载的 LLM 不在这里被卸载，只阻止重新加载

    rm.state["loaded"] = False
    rm._ext_cache = None
    p = Planner(rm.cfg, rm.store, rm.registry)
    if isinstance(p.backend, LMStudioBackend):
        p.resources = rm
        with pytest.raises(PlannerUnavailable, match="not loading"):
            p.backend.prepare()
    del fake.prompts["someone-else"]
    rm._ext_cache = None
    assert rm.llm_admit()[0]
