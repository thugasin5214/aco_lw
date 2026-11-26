import sys
import time

import pytest
from fastapi.testclient import TestClient

from video_agent.services import ServiceError, Services, validate


def _wait(cond, s=5.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_validate_rejects_bad_rows():
    with pytest.raises(ServiceError, match="bad id"):
        validate([{"id": "Bad Id", "start": "x"}])
    with pytest.raises(ServiceError, match="reserved"):
        validate([{"id": "aco", "start": "x"}])
    with pytest.raises(ServiceError, match="unknown"):
        validate([{"id": "a", "start": "x", "shell": "y"}])
    with pytest.raises(ServiceError, match="needs"):
        validate([{"id": "a"}])


def test_start_stop_only_what_aco_started(tmp_path):
    busy = {"n": 0}
    sv = Services(tmp_path, renders_busy=lambda: busy["n"])
    cmd = f"{sys.executable} -c 'import time; print(\"hello\", flush=True); time.sleep(60)'"
    sv.upsert({"id": "sleeper", "name": "Sleeper", "start": cmd, "protect": "renders"})
    sv.upsert({"id": "foreign", "port": 1})  # not started by aco, no stop command
    out = sv.start("sleeper")
    assert out["pid"] and sv.status(sv.spec("sleeper"))["up"]
    with pytest.raises(ServiceError, match="already running"):
        sv.start("sleeper")
    assert _wait(lambda: "hello" in sv.log("sleeper")["text"])
    busy["n"] = 1
    with pytest.raises(ServiceError, match="render"):
        sv.stop("sleeper")  # protect: renders
    busy["n"] = 0
    sv.stop("sleeper")
    assert sv.own_pid("sleeper") is None
    with pytest.raises(ServiceError, match="not started by aco"):
        sv.stop("foreign")
    sv.remove("foreign")
    assert [s["id"] for s in sv.specs()] == ["sleeper"]


def test_service_api_is_local_only(make_orch, fake):
    from video_agent.daemon import Actions
    from video_agent.planner import Planner
    from video_agent.studio import Studio
    from video_agent.web import create_app

    studio = Studio(make_orch(fake))
    actions = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    app = create_app(studio.orch.cfg, actions)
    local = TestClient(app, base_url="http://127.0.0.1:8790")
    ok = local.get("/api/services", headers={"X-ACO": "1"})
    assert ok.status_code == 200 and ok.json()["services"][0]["id"] == "aco"
    assert local.get("/api/services").status_code == 403  # no console header
    bad_origin = {"X-ACO": "1", "Origin": "https://evil.example"}
    assert local.post("/api/services", json={"remove": "x"}, headers=bad_origin).status_code == 403
    remote = TestClient(app, base_url="http://attacker.example")  # DNS rebinding
    assert remote.get("/api/services", headers={"X-ACO": "1"}).status_code == 403
    r = local.post(
        "/api/services",
        json={"service": {"id": "x", "health": "http://127.0.0.1:9/"}},
        headers={"X-ACO": "1"},
    )
    assert r.status_code == 200 and r.json()["services"][0]["id"] == "x"
    # aco without a Worker (va serve) cannot restart itself
    assert local.post("/api/services/aco/restart", headers={"X-ACO": "1"}).status_code == 409


def test_a_reused_pid_is_not_ours(tmp_path):
    import os

    sv = Services(tmp_path)
    sv.upsert({"id": "old", "port": 1, "start": "true"})
    sv.run_dir.mkdir(parents=True, exist_ok=True)
    # a pid file left by an earlier aco run; that pid now belongs to another process (ours here)
    (sv.run_dir / "old.pid").write_text(f"{os.getpid()} Mon Jan  1 00:00:00 2001\n")
    assert sv.own_pid("old") is None
    with pytest.raises(ServiceError, match="not started by aco"):
        sv.stop("old")
