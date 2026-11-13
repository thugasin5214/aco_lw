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


def test_delete_selected_tasks_skips_busy_and_production_use(studio):  # noqa: F811
    r1 = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    r2 = studio.start_round("flux2-t2i", {"prompt": "b", "seed": 10})
    drain(studio)
    st = studio.store
    t1 = [t["id"] for t in st.round_tasks(r1["id"])]
    t2 = [t["id"] for t in st.round_tasks(r2["id"])]
    prod = st.create_production(r1["project_id"], "film", "Film", {})
    st.add_artifact(prod["id"], "clips", "sh01", "review", round_id=r2["id"])
    queued = studio.start_round("flux2-t2i", {"prompt": "c", "seed": 20})  # 还没跑：排队中
    tq = st.round_tasks(queued["id"])[0]["id"]
    out = studio.delete_tasks([*t1, t2[0], tq, "nope"])
    assert out["tasks"] == 3 and out["rounds"] == 1  # r1 删空 → 轮也删
    assert out["skipped_busy"] == 1 and out["kept_for_productions"] == 1
    assert st.get_round(r1["id"]) is None and len(st.round_tasks(r2["id"])) == 3
    assert st.get(tq) is not None


def test_delete_project_removes_everything(studio, tmp_path):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    drain(studio)
    st = studio.store
    pid = rnd["project_id"]
    studio.select(rnd["id"], 1, role="主角")  # 基准包
    studio.add_memory("记住这个")
    studio.log("web", "in", "你好", pid)
    sess = studio.current_session(pid)
    run = st.create_run(pid, sess["id"], "web", "x")
    st.add_agent_msg(run["id"], "user", "x")
    st.update_run(run["id"], status="done")
    prod = st.create_production(pid, "film", "Film", {})
    st.add_artifact(prod["id"], "clips", "sh01", "current", round_id=rnd["id"])
    data = st.jobs_dir.parent
    for d in (
        data / "productions" / prod["id"],
        data / "agent-work" / run["id"],
        data / "uploads" / pid,
    ):
        d.mkdir(parents=True)
        (d / "f.bin").write_bytes(b"x" * 10)
    other = studio.new_project("别的项目")
    keep = studio.start_round("flux2-t2i", {"prompt": "z", "seed": 5}, project_id=other["id"])
    drain(studio)
    out = studio.delete_project(pid)
    assert out["tasks"] == 3 and out["productions"] == 1 and out["sessions"] >= 1
    assert st.get_project(pid) is None and st.rounds(pid) == [] and st.list(100, pid) == []
    assert st.sessions(pid) == [] and st.memories(pid) == [] and st.pack(pid) is None
    assert st.productions(pid) == [] and st.get_run(run["id"]) is None
    assert not (data / "productions" / prod["id"]).exists()
    assert not (data / "packs" / pid).exists() and not (data / "uploads" / pid).exists()
    assert len(st.round_tasks(keep["id"])) == 3  # 别的项目不动


def test_delete_project_refuses_while_busy(studio):  # noqa: F811
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})  # 排队中
    with pytest.raises(StudioError):
        studio.delete_project(rnd["project_id"])


def test_delete_project_api_needs_the_name(studio):  # noqa: F811
    from fastapi.testclient import TestClient

    from video_agent.daemon import Actions
    from video_agent.planner import Planner
    from video_agent.web import create_app

    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    drain(studio)
    a = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    c = TestClient(create_app(studio.orch.cfg, a))
    pid = rnd["project_id"]
    name = studio.store.get_project(pid)["name"]
    assert c.post(f"/api/projects/{pid}/delete", json={"confirm": "x"}).status_code == 400
    ids = [t["id"] for t in studio.store.round_tasks(rnd["id"])][:1]
    assert c.post("/api/tasks/delete", json={"ids": ids}).json()["tasks"] == 1
    assert c.post(f"/api/projects/{pid}/delete", json={"confirm": name}).status_code == 200
    assert studio.store.get_project(pid) is None
