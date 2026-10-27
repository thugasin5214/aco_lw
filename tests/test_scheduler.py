import json

import pytest
from conftest import FakeComfy

from video_agent.scheduler import DuplicateTask
from video_agent.store import TransitionError


def test_image_happy_path(make_orch, fake):
    o = make_orch(fake)
    task = o.create("flux2-t2i", {"prompt": "测试", "seed": 1})
    done = o.execute(task["id"])
    assert done["state"] == "review"
    assert fake.submits == 1
    assert [e["state"] for e in o.store.events(task["id"])] == [
        "draft",
        "validated",
        "waiting_memory",
        "submitted",
        "running",
        "review",
    ]
    assert done["outputs"][0]["kind"] == "image"
    assert done["metrics"]["exec_s"] == 11.4
    tdir = o.store.task_dir(task["id"])
    assert (tdir / done["outputs"][0]["file"]).read_bytes().startswith(b"\x89PNG")
    manifest = json.loads((tdir / "manifest.json").read_text())
    assert manifest["state"] == "review" and manifest["prompt_id"] == done["prompt_id"]
    assert (
        json.loads((tdir / "prompt.json").read_text())["16"]["inputs"]["filename_prefix"]
        == f"aco_lw/{task['id']}"
    )
    assert fake.frees == 0  # 图片模板 release_after=keep


def test_video_releases_after(make_orch, fake):
    o = make_orch(fake)
    t = o.create("h3-t2v", {"seed": 5})
    assert o.execute(t["id"])["state"] == "review"
    assert fake.frees == 1


def test_comfy_error_marks_failed(make_orch):
    fake = FakeComfy(fail=True)
    o = make_orch(fake)
    t = o.execute(o.create("flux2-t2i", {"seed": 1})["id"])
    assert t["state"] == "failed" and "OOM" in t["error"]


def test_duplicate_rejected_until_failed(make_orch, fake):
    o = make_orch(fake)
    o.execute(o.create("flux2-t2i", {"seed": 9})["id"])
    with pytest.raises(DuplicateTask):
        o.create("flux2-t2i", {"seed": 9})
    assert o.create("flux2-t2i", {"seed": 9}, allow_duplicate=True)


def test_memory_gate_timeout_never_submits(make_orch, fake):
    fake.busy_other = True  # 有人手动在跑，队列一直忙
    o = make_orch(fake)
    t = o.execute(o.create("flux2-t2i", {"seed": 2})["id"])
    assert t["state"] == "timeout" and fake.submits == 0


def test_illegal_transition(make_orch, fake):
    o = make_orch(fake)
    t = o.create("flux2-t2i", {"seed": 4})
    with pytest.raises(TransitionError):
        o.store.transition(t["id"], "done")


def test_recover_collects_without_resubmitting(make_orch, fake):
    """模拟提交后进程被杀：任务停在 submitted，重启后只对账。"""
    o = make_orch(fake)
    t = o.create("flux2-t2i", {"seed": 11})
    o.store.transition(t["id"], "validated")
    o.store.transition(t["id"], "waiting_memory")
    o.store.update(t["id"], prompt_id="11111111-1111-4111-8111-111111111111")
    o.comfy.submit({"x": {}}, "11111111-1111-4111-8111-111111111111")
    o.store.transition(t["id"], "submitted")
    # —— 崩溃，新进程 ——
    o2 = make_orch(fake)
    results = o2.recover()
    assert results[0][1] == "review"
    assert fake.submits == 1  # 只有崩溃前那一次


def test_recover_prompt_id_written_but_never_submitted(make_orch, fake):
    o = make_orch(fake)
    t = o.create("flux2-t2i", {"seed": 12})
    o.store.transition(t["id"], "validated")
    o.store.transition(t["id"], "waiting_memory")
    o.store.update(t["id"], prompt_id="22222222-2222-4222-8222-222222222222")
    results = o.recover()
    assert results == [(t["id"], "failed（找不到）")]
    assert fake.submits == 0
    # 失败后允许按相同参数重提
    assert o.create("flux2-t2i", {"seed": 12})


def test_parent_image_inherited(make_orch, fake):
    o = make_orch(fake)
    parent = o.execute(o.create("flux2-t2i", {"seed": 21})["id"])
    child = o.create("h3-i2v", {"seed": 1}, parent_id=parent["id"], allow_unverified=True)
    assert child["params"]["image"].endswith(parent["outputs"][0]["file"])
    assert o.execute(child["id"])["state"] == "review"


def test_timeout_scales_with_estimate_and_late_results_are_collected(make_orch):
    from conftest import FakeComfy

    fake = FakeComfy(history_after=4)
    o = make_orch(fake)
    o.cfg.task_timeout_s = 0  # 配置时限为 0 时，按预计耗时放大的时限仍然生效
    t = o.execute(o.create("flux2-t2i", {"seed": 31})["id"])
    assert t["state"] == "review"
    # 观察超时后才完成的任务：定期对账收回
    fake2 = FakeComfy(history_after=1000)
    o2 = make_orch(fake2)
    o2.cfg.task_timeout_s = 0
    o2.resources.estimator.estimate = lambda tpl, tier: type(
        "E", (), {"exec_s": 0.0, "peak_gib": 16, "family": "f", "source": "t"}
    )()
    t2 = o2.execute(o2.create("flux2-t2i", {"seed": 32})["id"])
    assert t2["state"] == "timeout" and o2.reconcile_timeouts() == []  # 还在跑：留到下次
    fake2.history_after = 0
    assert o2.reconcile_timeouts() == [(t2["id"], "review")]
