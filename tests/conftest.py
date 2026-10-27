"""测试夹具：假 ComfyUI（httpx.MockTransport）+ 临时数据目录；LLM 与内存读数全部打桩。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from video_agent import llm, resources
from video_agent.comfy import Comfy
from video_agent.config import ROOT, Settings
from video_agent.registry import Registry
from video_agent.scheduler import Orchestrator
from video_agent.store import Store


class FakeComfy:
    """最小 ComfyUI：/prompt 入队，history_after 次轮询后完成。"""

    def __init__(self, history_after: int = 2, fail: bool = False):
        self.history_after = history_after
        self.fail = fail
        self.prompts: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.submits = 0
        self.frees = 0
        self.busy_other = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/queue":
            running = [[0, pid, {}, {}, []] for pid in self.prompts if not self._done(pid)]
            if self.busy_other:
                running.append([0, "someone-else", {}, {}, []])
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if path == "/prompt":
            body = json.loads(req.content)
            self.submits += 1
            self.prompts[body["prompt_id"]] = body["prompt"]
            self.polls[body["prompt_id"]] = 0
            return httpx.Response(200, json={"prompt_id": body["prompt_id"]})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.prompts:
                return httpx.Response(200, json={})
            self.polls[pid] += 1
            if not self._done(pid):
                return httpx.Response(200, json={})
            return httpx.Response(200, json={pid: self._entry(pid)})
        if path == "/object_info":
            return httpx.Response(
                200, json=json.loads((Path(__file__).parent / "object_info_min.json").read_text())
            )
        if path == "/free":
            self.frees += 1
            return httpx.Response(200, json={})
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "up.png", "subfolder": "", "type": "input"})
        if path == "/view":
            return httpx.Response(200, content=b"\x89PNG fake")
        return httpx.Response(404)

    def _done(self, pid: str) -> bool:
        return self.polls.get(pid, 0) >= self.history_after

    def _entry(self, pid: str) -> dict:
        if self.fail:
            return {
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [
                        ["execution_start", {"timestamp": 1000}],
                        ["execution_error", {"timestamp": 3000, "exception_message": "OOM"}],
                    ],
                },
                "outputs": {},
            }
        return {
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [
                    ["execution_start", {"timestamp": 1000}],
                    ["execution_success", {"timestamp": 12400}],
                ],
            },
            "outputs": {
                "16": {"images": [{"filename": "a.png", "subfolder": "x", "type": "output"}]}
            },
        }


@pytest.fixture
def no_side_effects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("测试不应调用真实 lms")

    monkeypatch.setattr(llm, "loaded", lambda model: None)
    monkeypatch.setattr(llm, "unload", boom)
    monkeypatch.setattr(llm, "load", boom)
    monkeypatch.setattr(
        resources,
        "snapshot",
        lambda: {"avail_gib": 100.0, "used_gib": 20.0, "swap_used_gib": 0.0, "pressure": 1},
    )


@pytest.fixture
def fake():
    return FakeComfy()


@pytest.fixture
def make_orch(tmp_path, no_side_effects):
    def _make(fake_comfy: FakeComfy) -> Orchestrator:
        cfg = Settings(
            data_dir=tmp_path,
            templates_dir=ROOT / "templates",
            poll_interval_s=0,
            memory_wait_s=0.0,
        )
        cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
        comfy = Comfy("http://fake", transport=httpx.MockTransport(fake_comfy.handler))
        return Orchestrator(
            cfg,
            comfy=comfy,
            store=Store(cfg.db_path, cfg.jobs_dir),
            registry=Registry.from_settings(cfg),
            log=lambda m: None,
            sleep=lambda s: None,
        )

    return _make
