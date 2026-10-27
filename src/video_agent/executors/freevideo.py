"""FreeVideo 执行端：FreeVideo 电影的 take 由 FreeVideo agent API 自己排队拍摄（见 production.py 的 film 阶段），
这里不提交任何东西，只让资源闸门看得见 FreeVideo 自己的队列（拍片时不加载 LLM、不抢内存）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Poll, Prepared


class FreeVideoExecutor:
    name = "freevideo"

    def __init__(self, client):
        self.client = client  # video_agent.freevideo.FreeVideo
        self._last: dict = {}

    def _queue(self) -> dict:
        try:
            q = self.client.status().get("queue") or {}
        except Exception:  # noqa: BLE001 — FreeVideo 不可达：不当作忙
            q = {}
        self._last = q
        return q

    def prepare(self, template, task: dict) -> Prepared:
        raise RuntimeError(
            "FreeVideo takes are shot by the FreeVideo film API, not submitted by the worker"
        )

    def submit(self, prepared: Prepared, job_id: str) -> None:
        raise RuntimeError("not supported")

    def poll(self, job_id: str) -> Poll:
        return Poll("lost")

    def failure_detail(self, result: Any) -> str:
        return "FreeVideo take"

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        return []

    def cancel(self, job_id: str) -> None:
        return None

    def queued_payloads(self) -> list[dict]:
        """FreeVideo 正在拍 / 排着：报告成一个独占的视频任务，跨进程闸门据此不加载 LLM。"""
        q = self._queue()
        if (q.get("running") or 0) + (q.get("pending") or 0) > 0:
            return [{"freevideo": {"class_type": "FreeVideoGenerate", "inputs": {}}}]
        return []

    def busy(self) -> bool:
        q = self._queue()
        return (q.get("running") or 0) + (q.get("pending") or 0) > 0

    def release(self) -> bool:
        return True
