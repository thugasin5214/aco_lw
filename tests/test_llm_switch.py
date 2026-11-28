"""Switching the local LLM (/llm, `va llm`, run_command) and commands with a memory estimate."""

import threading

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — env is a fixture
from test_resources import Ex, rm  # noqa: F401 — rm is a fixture
from test_toolbox import pending_approval

from video_agent import llm
from video_agent.agent import command_estimate
from video_agent.llm_control import LLMControl, LLMControlError

GIB = 2**30
MODELS = [
    {"key": "qwen3.8-27b-splash", "size": 16 * GIB, "vision": True},
    {"key": "qwen3.5-122b-a10b", "size": 70 * GIB, "vision": True},
    {"key": "q27b-dpo-mine", "size": 15 * GIB, "vision": False},
]


@pytest.fixture
def lms(monkeypatch):
    """A fake LM Studio: which models are loaded, plus load / unload calls."""
    state = {"loaded": {"qwen3.8-27b-splash"}, "calls": []}
    monkeypatch.setattr(llm, "models", lambda: MODELS)
    monkeypatch.setattr(
        llm, "loaded", lambda m: {"identifier": m} if m in state["loaded"] else None
    )
    monkeypatch.setattr(
        llm, "loaded_all", lambda: [{"identifier": m} for m in sorted(state["loaded"])]
    )

    def unload(m):
        state["calls"].append(("unload", m))
        state["loaded"].discard(m)

    def load(m, ctx, ttl=None, gpu="", par=0):
        state["calls"].append(("load", m))
        state["loaded"].add(m)

    monkeypatch.setattr(llm, "unload", unload)
    monkeypatch.setattr(llm, "load", load)
    return state


def control(rm, avail):  # noqa: F811
    rm.snap = lambda: {"avail_gib": avail, "used_gib": 0, "swap_used_gib": 0, "pressure": 1}

    class P:
        mode = "on_demand"

    rm.cfg.llm_model = "qwen3.8-27b-splash"
    return LLMControl(rm.cfg, rm, P())


def test_use_unloads_the_previous_model_and_loads_the_new_one(rm, lms):  # noqa: F811
    ctl = control(rm, avail=80)
    assert ctl.resolve("122b") == "qwen3.5-122b-a10b" and ctl.resolve("3") == "q27b-dpo-mine"
    with pytest.raises(LLMControlError, match="ambiguous"):
        ctl.resolve("q")
    out = ctl.use("122b")
    assert out["state"] == "working" and out["need_gib"] == 87.5  # 70 GiB × 1.25 (estimate)
    assert ctl.wait(5)["state"] == "idle"
    assert lms["calls"] == [("unload", "qwen3.8-27b-splash"), ("load", "qwen3.5-122b-a10b")]
    assert rm.cfg.llm_model == "qwen3.5-122b-a10b"


def test_use_refuses_without_memory_or_during_a_video(rm, lms):  # noqa: F811
    ctl = control(rm, avail=60)  # 60 + 20 (unloading the 27B) < 87.5 + 8
    with pytest.raises(LLMControlError, match="not enough memory"):
        ctl.use("122b")
    assert rm.cfg.llm_model == "qwen3.8-27b-splash" and lms["calls"] == []
    ctl = control(rm, avail=200)
    rm.films.add("film1")
    with pytest.raises(LLMControlError, match="FreeVideo"):
        ctl.use("122b")


def test_command_estimate():
    assert command_estimate("python train.py", {"memory_gib": 40, "exclusive": True}) == {
        "gib": 40.0,
        "exclusive": True,
        "minutes": None,
        "source": "agent",
        "gate": True,
    }
    size = lambda name: 87.5
    est = command_estimate("lms load qwen3.5-122b-a10b", {"memory_gib": 10}, size)
    assert est["gib"] == 87.5 and est["source"] == "model size" and est["gate"]
    est = command_estimate("va llm use 122b", {}, size)
    assert est["gate"] is False and est["gib"] == 87.5  # the daemon gates `va llm` itself


def test_command_waits_for_a_video_then_holds_the_memory(rm):  # noqa: F811
    rm.snap = lambda: {"avail_gib": 100, "used_gib": 0, "swap_used_gib": 0, "pressure": 1}
    reg = rm.registry
    assert rm.prepare_render(reg.template("h3-t2v"), "smoke", Ex(), "t1").ok
    out = {}
    rm.sleep = lambda s: threading.Event().wait(0.01)
    t = threading.Thread(
        target=lambda: out.update(rm.admit_command("k", 40, True, "train", timeout_s=10))
    )
    t.start()
    t.join(0.2)
    assert t.is_alive()  # a video holds the memory: the command waits
    rm.render_done(reg.template("h3-t2v"), released=True, unloaded_llm=False)
    t.join(5)
    assert out["ok"] and "k" in rm.commands
    ok, why = rm.llm_admit()
    assert not ok and "train" in why  # an exclusive command keeps the LLM from loading
    woke = []
    rm.listeners.append(lambda: woke.append(1))
    rm.command_done("k")
    assert rm.llm_admit()[0] and woke == [1]


def test_command_gives_up_when_memory_never_frees(rm):  # noqa: F811
    rm.snap = lambda: {"avail_gib": 10, "used_gib": 0, "swap_used_gib": 0, "pressure": 1}
    rm.state["loaded"] = False
    out = rm.admit_command("k", 40, False, "big", timeout_s=0)
    assert not out["ok"] and "needs 48.0 GiB" in out["reason"] and "k" not in rm.commands


def test_llm_commands_need_approval_and_show_the_estimate(env, lms):  # noqa: F811
    studio, make, said = env
    a, _ = make(
        {
            "tool_calls": [
                call(
                    "run_command",
                    {
                        "command": "python big.py",
                        "reason": "x",
                        "memory_gib": 30,
                        "exclusive": True,
                    },
                )
            ]
        },
        {"text": "ok"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert run["status"] == "waiting_user" and pending_approval(studio, run["id"])
    text = next(t for t, b in said if b)
    assert "30.0 GiB" in text and "exclusive" in text.lower()


def test_telegram_llm_command(env, lms, monkeypatch):  # noqa: F811
    studio, make, _said = env
    a, _ = make()
    rmr = studio.orch.resources
    rmr.snap = lambda: {"avail_gib": 200, "used_gib": 0, "swap_used_gib": 0, "pressure": 1}
    out = []
    reply = lambda t, b=None: out.append(t)
    a.inbound("telegram", "/llm", reply)
    assert "qwen3.5-122b-a10b" in out[-1] and "1. qwen3.8-27b-splash" in out[-1]
    a.inbound("telegram", "/llm use dpo", reply)
    a.llm_control.wait(5)
    for _ in range(50):
        if "q27b-dpo-mine" in out[-1] and ("loaded" in out[-1] or "已加载" in out[-1]):
            break
        threading.Event().wait(0.05)
    assert studio.orch.cfg.llm_model == "q27b-dpo-mine"
    assert ("unload", "qwen3.8-27b-splash") in lms["calls"]
