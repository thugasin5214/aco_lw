"""R1 + Q1: resource gating, learned estimates, queue policy."""

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
    assert gate.ok and gate.unloaded_llm  # video is exclusive: unload the LLM
    ok, why = rm.llm_admit()
    assert not ok and "video" in why  # while a video renders, the LLM cannot load
    called = []
    rm.listeners.append(lambda: called.append(1))
    rm.render_done(reg.template("h3-t2v"), released=True, unloaded_llm=False)
    assert rm.llm_admit()[0] and called == [1]  # render finished → wake the waiters
    rm.state["loaded"] = True
    gate = rm.prepare_render(reg.template("flux2-t2i"), "standard", Ex(), "t2")
    # images can coexist with the LLM
    assert gate.ok and not gate.unloaded_llm and rm.llm_admit()[0]


def test_render_waits_for_answer_in_progress(rm):
    """A video render arriving while the LLM is writing an answer: wait for the answer, then unload; never interrupt."""
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
    assert rm.choose_next(drafts)["id"] == "you"  # your request first
    drafts = [d("img", "flux2-t2i", 1), d("vid", "h3-t2v", 1)]
    assert rm.choose_next(drafts)["id"] == "img"  # same family first, saves a switch
    drafts = [d("new", "flux2-t2i", 1), d("old-bg", "flux2-t2i", 0, age=3600)]
    # background tasks that waited too long do not starve
    assert rm.choose_next(drafts)["id"] == "old-bg"


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
    e = rm.estimator.estimate(t, "preview")  # 896×512 vs 1280×720 extrapolated by cost ratio
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
    assert plan[1]["start"] - plan[0]["end"] == pytest.approx(15.0)  # family switch cost
    assert rm.resident_family == "flux2-klein-4b-q8"  # the simulation does not change shared state


def test_cross_process_gate_sees_other_peoples_video(rm, fake):
    """Videos submitted by another process / by you in the ComfyUI page must also block LLM loading (cross-process gate)."""
    from video_agent.backends import LMStudioBackend
    from video_agent.planner import Planner, PlannerUnavailable

    video_graph = {"3": {"class_type": "FreeVideoGenerate", "inputs": {}}}
    fake.prompts["someone-else"] = video_graph  # not submitted by this system
    rm.state["loaded"] = False
    ok, why = rm.llm_admit()
    assert not ok and "video render" in why
    rm.state["loaded"] = True
    rm._ext_cache = None
    # an already-loaded LLM is not unloaded here; this only blocks reloading
    assert rm.llm_admit()[0]

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


def test_peak_is_per_tier_and_adaptive_uses_registry(rm):
    import json as _j

    def done(tpl, tier, exec_s, peak_gib):
        t = rm.store.create(
            template=tpl,
            template_sha="x",
            kind="video",
            tier=tier,
            params={},
            params_hash=f"{tier}{exec_s}",
        )
        rm.store.update(
            t["id"], metrics={"exec_s": exec_s, "freevideo": {"ram_peak_bytes": peak_gib * 2**30}}
        )
        for st in ("validated", "waiting_memory", "submitted", "review"):
            rm.store.transition(t["id"], st)

    done("h3-i2v", "smoke", 320, 54.9)
    done("h3-i2v", "hd-60s", 18908, 98.4)
    t = rm.registry.template("h3-i2v")
    # adaptive: uses the registry value, not raised by 98.4
    assert rm.estimator.estimate(t, "smoke").peak_gib == 54
    assert rm.estimator.estimate(t, "hd-60s").exec_s == 18908
    rm.registry.models["freevideo-h3"].adaptive = False
    # not adaptive: this tier's measurement
    assert rm.estimator.estimate(t, "smoke").peak_gib == 54.9
    assert _j


def test_film_shooting_unloads_llm_and_blocks_it(rm):
    """FreeVideo films skip prepare_render: unload the LLM before shooting, no loading while shooting, wake the waiters when done."""
    acts = rm.prepare_film()
    assert rm.state["unloads"] == 1 and acts
    rm.film_started("f1")
    ok, why = rm.llm_admit()
    assert not ok and "film" in why
    # the LLM got loaded elsewhere mid-shoot (or the shoot continued after a daemon restart)
    rm.state["loaded"] = True
    rm.films_shooting({"f1"})
    assert rm.state["unloads"] == 2 and not rm.llm_admit()[0]
    called = []
    rm.listeners.append(lambda: called.append(1))
    rm.films_shooting(set())
    assert rm.llm_admit()[0] and called == [1]


def test_prepare_film_does_not_interrupt_an_answer(rm, monkeypatch):
    from video_agent import resources

    monkeypatch.setattr(resources, "ANSWER_WAIT_S", 0.01)
    with rm.llm.inference(), pytest.raises(resources.ResourceBusy):
        rm.prepare_film()
    assert rm.state["unloads"] == 0
