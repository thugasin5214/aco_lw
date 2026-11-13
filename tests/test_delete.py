"""删除产物：记录 + data/jobs + ComfyUI output 里本系统的原图；正在跑 / 制片在用的不删。"""

import pytest
from test_studio import drain, studio  # noqa: F401 — studio 是夹具

from video_agent.studio import StudioError


def comfy_originals(studio, tmp_path, tasks):  # noqa: F811
    """让产物记录指向一个假的 ComfyUI output 目录，并放一个别人的文件。"""
    out = tmp_path / "comfy_output"
    (out / "aco_lw").mkdir(parents=True)
    studio.orch.cfg.comfy_output_dir = str(out)
    files = []
    for t in tasks:
        name = f"{t['id']}_00001_.png"
        (out / "aco_lw" / name).write_bytes(b"png")
        outs = [
            {
                **t["outputs"][0],
                "source": {"filename": name, "subfolder": "aco_lw", "type": "output"},
            }
        ]
        studio.store.update(t["id"], outputs=outs)
        files.append(out / "aco_lw" / name)
    other = out / "someone_else.png"
    other.write_bytes(b"x")
    return files, other


def test_delete_round_removes_records_and_files(studio, tmp_path):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 1})
    drain(studio)
    tasks = studio.store.round_tasks(rnd["id"])
    files, other = comfy_originals(studio, tmp_path, tasks)
    dirs = [studio.store.task_dir(t["id"]) for t in tasks]
    assert all(d.is_dir() for d in dirs)
    out = studio.delete_round(rnd["id"])
    assert out["rounds"] == 1 and out["tasks"] == 3 and out["freed_mb"] >= 0
    assert studio.store.get_round(rnd["id"]) is None
    assert all(studio.store.get(t["id"]) is None for t in tasks)
    assert not any(d.exists() for d in dirs) and not any(f.exists() for f in files)
    assert other.exists()  # 不是本系统生成的：不动


def test_delete_task_keeps_round_until_empty(studio):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 1})
    drain(studio)
    a, b, c = studio.store.round_tasks(rnd["id"])
    studio.delete_task(a["id"])
    studio.delete_task(b["id"])
    assert studio.store.get_round(rnd["id"]) is not None
    assert studio.delete_task(c["id"])["rounds"] == 1
    assert studio.store.get_round(rnd["id"]) is None


def test_busy_tasks_are_not_deleted(studio):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 1})  # 还在排队
    with pytest.raises(StudioError):
        studio.delete_round(rnd["id"])
    assert len(studio.store.round_tasks(rnd["id"])) == 3


def test_purge_review_only_and_unselected_keeps_approved(studio):  # noqa: F811
    r1 = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    r2 = studio.start_round("flux2-t2i", {"prompt": "b", "seed": 10})
    drain(studio)
    studio.select(r1["id"], 2)  # r1：#2 通过，其余打回；r2 仍待审阅
    pid = r1["project_id"]
    out = studio.purge_project(pid, "review")
    assert out["tasks"] == 3 and out["rounds"] == 1 and studio.store.get_round(r2["id"]) is None
    assert len(studio.store.round_tasks(r1["id"])) == 3  # 已决定的这轮没动
    out = studio.purge_project(pid, "unselected")
    left = studio.store.round_tasks(r1["id"])
    assert out["tasks"] == 2 and [t["state"] for t in left] == ["done"]
    assert studio.store.get_round(r1["id"]) is not None  # 还有通过的那张：轮次保留


def test_active_production_blocks_delete(studio, monkeypatch):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 1})
    drain(studio)
    monkeypatch.setattr(
        studio.store,
        "active_production_refs",
        lambda rids, tids: (
            [
                {
                    "round_id": rnd["id"],
                    "task_id": None,
                    "status": "review",
                    "production_title": "Film",
                }
            ]
            if rnd["id"] in rids
            else []
        ),
    )
    with pytest.raises(StudioError, match="Film"):
        studio.delete_round(rnd["id"])
    out = studio.purge_project(rnd["project_id"], "review")
    assert out["tasks"] == 0 and out["kept_for_productions"] == 1


def test_active_production_refs_query(studio):  # noqa: F811
    assert studio.store.active_production_refs(["r"], []) == []
    assert studio.store.active_production_refs([], ["t"]) == []


def test_old_versions_in_an_active_production_do_not_block_purge(studio):  # noqa: F811
    failed = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    used = studio.start_round("flux2-t2i", {"prompt": "b", "seed": 10})
    waiting = studio.start_round("flux2-t2i", {"prompt": "c", "seed": 20})
    drain(studio)
    studio.select(used["id"], 2)
    pick = studio.store.get_round(used["id"])["decision"]["task_id"]
    pid = failed["project_id"]
    st = studio.store
    prod = st.create_production(pid, "film", "Film", {})
    assert prod["state"] == "active"
    st.add_artifact(prod["id"], "clips", "sh01", "failed", round_id=failed["id"])
    st.add_artifact(prod["id"], "clips", "sh01", "current", round_id=used["id"], task_id=pick)
    st.add_artifact(prod["id"], "clips", "sh02", "review", round_id=waiting["id"])
    out = studio.purge_project(pid, "unselected")
    assert st.get_round(failed["id"]) is None  # 制片里失败的旧版本：照删
    assert [t["id"] for t in st.round_tasks(used["id"])] == [pick]  # 只留制片用的那张
    assert len(st.round_tasks(waiting["id"])) == 3  # 制片还在审阅：整轮保留
    assert out["kept_for_productions"] == 2 and out["kept_productions"] == ["Film"]


def test_clear_review_also_removes_review_rounds_whose_candidates_all_failed(studio, fake):  # noqa: F811
    fake.fail = True
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    drain(studio)
    st = studio.store
    assert st.get_round(rnd["id"])["state"] == "review"
    assert {t["state"] for t in st.round_tasks(rnd["id"])} == {"failed"}
    out = studio.purge_project(rnd["project_id"], "review")
    assert out["rounds"] == 1 and st.get_round(rnd["id"]) is None
