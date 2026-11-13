"""freevideo-ref: reference video (guiding only the first K steps) + reference images → a FreeVideo generate job; the orchestrator submits, polls, reconciles."""

import json
from datetime import UTC, datetime

import pytest

from video_agent.executors.freevideo import FreeVideoExecutor, build_job


class FakeFV:
    def __init__(self):
        self.calls, self.status = [], "running"

    def upload(self, path):
        return f"assets/{path.name}"

    def call(self, method, path, body=None, **params):
        self.calls.append((method, path, body))
        if path == "/jobs":
            return {"job": {"id": "j000000000001", "status": "queued"}}
        if path.startswith("/jobs/") and method == "GET":
            job = {"id": "j000000000001", "status": self.status, "seed": 7}
            if self.status == "succeeded":
                job["result"] = {"video": {"url": "/lib/x/file"}}
            if self.status == "failed":
                job["error"] = {"error_zh": "显存不足"}
            return {"job": job}
        return {}

    def download(self, url, dest):
        dest.write_bytes(b"mp4")
        return dest


class T:
    name = "freevideo-ref"


def test_build_job_orders_video_before_pictures_and_keeps_early_steps():
    job = build_job(
        {
            "prompt": "p",
            "width": 960,
            "height": 544,
            "seconds": 5,
            "quality": "light",
            "early_steps": 3,
            "seed": 1,
        },
        {"ref_image": "img.png", "ref_video": "v.mp4"},
        platform="linux",
    )
    assert job["media"]["references"] == [{"file": "v.mp4", "early_steps": 3}, {"file": "img.png"}]
    assert job["kind"] == "generate" and job["seed"] == 1
    with pytest.raises(ValueError):
        build_job({"prompt": "p", "width": 1, "height": 1, "seconds": 1, "quality": "ultra"}, {})


def test_submit_poll_collect_survive_restart(tmp_path):
    v, img = tmp_path / "d.mp4", tmp_path / "c.png"
    v.write_bytes(b"v")
    img.write_bytes(b"i")
    fv, jobs = FakeFV(), tmp_path / "jobs.json"
    ex = FreeVideoExecutor(fv, jobs)
    params = {
        "prompt": "p",
        "width": 960,
        "height": 544,
        "seconds": 5,
        "quality": "light",
        "ref_video": str(v),
        "ref_image": str(img),
    }
    prep = ex.prepare(T(), {"params": params})
    ex.submit(prep, "task-1")
    sent = fv.calls[-1][2]
    assert sent["ref"] == "task-1" and "early_steps" not in sent["media"]["references"][0]
    ex2 = FreeVideoExecutor(fv, jobs)  # daemon restart: keep reconciling from the mapping on disk
    assert ex2.poll("task-1").state == "running"
    assert ex2.poll("unknown").state == "lost"
    fv.status = "succeeded"
    p = ex2.poll("task-1")
    assert p.state == "done"
    metrics: dict = {}
    out = ex2.collect("task-1", p.result, tmp_path, metrics)
    assert out[0]["file"] == "video.mp4" and (tmp_path / "video.mp4").exists()
    assert metrics["freevideo"]["seed"] == 7
    fv.status = "failed"
    p = ex2.poll("task-1")
    assert p.state == "failed" and p.detail == "显存不足"
    ex2.cancel("task-1")
    assert fv.calls[-1][:2] == ("POST", "/jobs/j000000000001/cancel")
    assert json.loads(jobs.read_text()) == {"task-1": "j000000000001"}


def test_rejects_wrong_media_kind(tmp_path):
    img = tmp_path / "c.png"
    img.write_bytes(b"i")
    ex = FreeVideoExecutor(FakeFV())
    with pytest.raises(ValueError):
        ex.prepare(T(), {"params": {"prompt": "p", "ref_video": str(img)}})


def test_takes_are_still_not_submitted_by_the_worker():
    class Take:
        name = "freevideo-take"

    with pytest.raises(RuntimeError):
        FreeVideoExecutor(FakeFV()).prepare(Take(), {"params": {}})


def test_poll_errors_do_not_crash_the_worker(tmp_path):
    from video_agent.freevideo import FreeVideoError

    class Flaky(FakeFV):
        def call(self, method, path, body=None, **params):
            if method == "GET":
                raise self.err
            return super().call(method, path, body, **params)

    fv = Flaky()
    ex = FreeVideoExecutor(fv, tmp_path / "jobs.json")
    ex._remember("t", "j000000000001")
    fv.err = OSError("connection reset")
    assert ex.poll("t").state == "running"
    fv.err = FreeVideoError(404, {"error": "not_found"})
    assert ex.poll("t").state == "lost"


def test_early_steps_rejected_on_mac_before_hours_of_encoding():
    params = {"prompt": "p", "width": 960, "height": 544, "seconds": 5, "early_steps": 3}
    with pytest.raises(ValueError, match="Mac"):
        build_job(params, {"ref_video": "v.mp4"}, platform="darwin")
    assert "early_steps" not in build_job({**params, "early_steps": 0}, {"ref_video": "v.mp4"},
                                          platform="darwin")["media"]["references"][0]


def test_heartbeat_marks_job_alive():
    from video_agent.executors.freevideo import _alive

    now = 1_800_000_000.0
    fresh = datetime.fromtimestamp(now - 30, tz=UTC).isoformat().replace("+00:00", "Z")
    stale = datetime.fromtimestamp(now - 3600, tz=UTC).isoformat()
    assert _alive({"last_heartbeat_at": fresh}, now)
    assert not _alive({"last_heartbeat_at": stale}, now)
    assert not _alive({}, now) and not _alive({"last_heartbeat_at": "garbage"}, now)
