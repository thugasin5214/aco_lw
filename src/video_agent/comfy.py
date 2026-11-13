"""Thin wrapper over the ComfyUI HTTP API. Only the orchestrator calls it."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx


class ComfyBusy(RuntimeError):
    pass


class Comfy:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=base_url, timeout=30, transport=transport)
        self.client_id = str(uuid.uuid4())

    def close(self) -> None:
        self.http.close()

    def _get(self, route: str, **params) -> dict:
        r = self.http.get(route, params=params or None)
        r.raise_for_status()
        return r.json()

    def queue(self) -> dict:
        return self._get("/queue")

    def queue_busy(self) -> bool:
        q = self.queue()
        return bool(q["queue_running"] or q["queue_pending"])

    def in_queue(self, prompt_id: str) -> bool:
        q = self.queue()
        return any(item[1] == prompt_id for item in q["queue_running"] + q["queue_pending"])

    def system_stats(self) -> dict:
        return self._get("/system_stats")

    def object_info(self) -> dict:
        r = self.http.get("/object_info", timeout=120)
        r.raise_for_status()
        return r.json()

    def history(self, prompt_id: str) -> dict | None:
        return self._get(f"/history/{prompt_id}").get(prompt_id)

    def submit(self, graph: dict, prompt_id: str) -> str:
        """prompt_id is generated and stored by the caller first, so after a crash we can reconcile by it instead of resubmitting."""
        body = {"prompt": graph, "client_id": self.client_id, "prompt_id": prompt_id}
        r = self.http.post("/prompt", json=body)
        if r.status_code == 400:
            raise ValueError(f"ComfyUI 拒绝了工作流：{r.text[:2000]}")
        r.raise_for_status()
        return r.json()["prompt_id"]

    def upload_image(self, path: Path) -> str:
        with path.open("rb") as f:
            r = self.http.post(
                "/upload/image", files={"image": (path.name, f)}, data={"overwrite": "false"}
            )
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def free(self) -> None:
        """Release only after the queue is seen empty twice; otherwise raise ComfyBusy. Never cancels any task."""
        if self.queue_busy():
            raise ComfyBusy("ComfyUI 队列非空，拒绝释放")
        if self.queue_busy():
            raise ComfyBusy("ComfyUI 队列在确认间隙变为非空，拒绝释放")
        self.http.post("/free", json={"unload_models": True, "free_memory": True})

    def interrupt(self, prompt_id: str) -> None:
        """Interrupt / remove only the given prompt_id (our own task); other people's tasks are untouched."""
        self.http.post("/interrupt", json={"prompt_id": prompt_id})
        self.http.post("/queue", json={"delete": [prompt_id]})

    def download(self, filename: str, subfolder: str, type_: str, dest: Path) -> Path:
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        with self.http.stream("GET", "/view", params=params, timeout=300) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest
