"""FreeVideo executor (agent API).

- `freevideo-take`: a film's takes are queued and shot by FreeVideo itself (see the film stage in production.py); nothing is submitted here,
  we only let the resource gate see FreeVideo's own queue (no LLM loading and no memory grabbing while shooting).
- Other templates (e.g. `freevideo-ref`): build one generate job for `POST /jobs` — reference video (optionally guiding only the first K steps),
  reference images, first frame — submitted, polled and downloaded by the orchestrator. The orchestrator's job_id is the idempotency key `ref`; FreeVideo's job ID
  is recorded in `jobs_file`, so the daemon can still reconcile after a restart (never resubmits automatically).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import Poll, Prepared

QUALITIES = ("light", "medium", "high", "max")
DONE = {"succeeded": "done", "failed": "failed", "cancelled": "failed", "interrupted": "failed"}
VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
HEARTBEAT_FRESH_S = 600


def _alive(job: dict, now: float | None = None) -> bool:
    """A heartbeat within HEARTBEAT_FRESH_S seconds = the job is still progressing."""
    hb = job.get("last_heartbeat_at")
    if not hb:
        return False
    try:
        ts = datetime.fromisoformat(hb).timestamp()
    except ValueError:
        return False
    return (now if now is not None else time.time()) - ts < HEARTBEAT_FRESH_S


def build_job(params: dict, media: dict[str, str], platform: str = sys.platform) -> dict:
    """Parameters + uploaded media names → a generate job (pure function, easy to test).
    The order of references decides the numbering in the prompt: <Video 1> is the reference video, <Picture 1>/<Picture 2> the reference images in order."""
    q = params.get("quality", "light")
    if q not in QUALITIES:
        raise ValueError(f"quality 只能是 {'/'.join(QUALITIES)}，收到 {q}")
    refs: list = []
    if params.get("early_steps") and platform == "darwin":
        # FreeVideo's Mac encoding path does not implement early_steps: it encodes for hours and
        # then reports
        # "early_steps requires separately encoded early reference conditions" (measured 2026-11-07)
        raise ValueError("early_steps 在 Mac 上 FreeVideo 不支持；去掉 early_steps（参考视频引导全部步数）")
    if "ref_video" in media:
        v: dict = {"file": media["ref_video"]}
        if params.get("early_steps"):
            v["early_steps"] = int(params["early_steps"])
        refs.append(v)
    refs += [{"file": media[k]} for k in ("ref_image", "ref_image2") if k in media]
    job: dict = {
        "kind": "generate",
        "prompt": params["prompt"],
        "width": int(params["width"]),
        "height": int(params["height"]),
        "seconds": float(params["seconds"]),
        "quality": q,
    }
    if params.get("seed") is not None:
        job["seed"] = int(params["seed"])
    if refs:
        job["media"] = {"references": refs}
    return job


class FreeVideoExecutor:
    name = "freevideo"

    def __init__(self, client, jobs_file: Path | None = None):
        self.client = client  # video_agent.freevideo.FreeVideo
        self._last: dict = {}
        self.jobs_file = jobs_file
        self._lock = threading.Lock()

    # ---- orchestrator job_id → FreeVideo job ID (on disk, reconciled after restart) ----
    def _jobs(self) -> dict:
        if self.jobs_file and self.jobs_file.exists():
            return json.loads(self.jobs_file.read_text())
        return {}

    def _remember(self, job_id: str, fv_id: str) -> None:
        if not self.jobs_file:
            return
        with self._lock:
            jobs = self._jobs()
            jobs[job_id] = fv_id
            self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
            self.jobs_file.write_text(json.dumps(jobs))

    def _queue(self) -> dict:
        try:
            q = self.client.status().get("queue") or {}
        except Exception:  # noqa: BLE001 — FreeVideo unreachable: not treated as busy
            q = {}
        self._last = q
        return q

    def prepare(self, template, task: dict) -> Prepared:
        if template.name == "freevideo-take":
            raise RuntimeError(
                "FreeVideo takes are shot by the FreeVideo film API, not submitted by the worker"
            )
        params = dict(task["params"])
        media: dict[str, str] = {}
        for name in ("ref_video", "ref_image", "ref_image2"):
            if name not in params:
                continue
            path = Path(params[name]).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"{name}: 找不到文件 {path}")
            if (name == "ref_video") != (path.suffix.lower() in VIDEO_EXT):
                raise ValueError(f"{name}: 文件类型不对 {path.name}")
            media[name] = self.client.upload(path)
        job = build_job(params, media)
        return Prepared(
            payload=job,
            uploads=media,
            artifacts={"job.json": json.dumps(job, ensure_ascii=False, indent=2)},
            note=f"FreeVideo generate · {len(media)} 个参考 · {job['quality']}",
        )

    def submit(self, prepared: Prepared, job_id: str) -> None:
        out = self.client.call("POST", "/jobs", {**prepared.payload, "ref": job_id})
        job = out.get("job") or out
        self._remember(job_id, job["id"])

    def poll(self, job_id: str) -> Poll:
        fv_id = self._jobs().get(job_id)
        if not fv_id:
            return Poll("lost")
        try:
            job = self.client.call("GET", f"/jobs/{fv_id}").get("job")
        # 404 = not in FreeVideo; anything else (network hiccup) ask again next time
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 404:
                return Poll("lost", str(e))
            return Poll("running", f"poll error: {e}")
        st = job.get("status")
        if st in DONE:
            err = job.get("error") or {}
            detail = err.get("error_zh") or err.get("message") if isinstance(err, dict) else err
            return Poll(DONE[st], detail or (None if st == "succeeded" else st), job)
        return Poll("running" if st == "running" else "queued", st, job, alive=_alive(job))

    def failure_detail(self, result: Any) -> str:
        if isinstance(result, dict):
            err = result.get("error") or {}
            if isinstance(err, dict):
                return err.get("error_zh") or err.get("message") or str(result.get("status"))
            return str(err)
        return "FreeVideo job"

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        video = ((result or {}).get("result") or {}).get("video") or {}
        if not video.get("url"):
            return []
        self.client.download(video["url"], dest / "video.mp4")
        metrics["freevideo"] = {
            k: result.get(k) for k in ("id", "seconds", "seed", "quality") if k in result
        }
        return [{"file": "video.mp4", "kind": "video", "source": {"job": result.get("id")}}]

    def cancel(self, job_id: str) -> None:
        fv_id = self._jobs().get(job_id)
        if fv_id:  # cancel only the one we submitted
            self.client.call("POST", f"/jobs/{fv_id}/cancel", {})

    def queued_payloads(self) -> list[dict]:
        """FreeVideo shooting / queued: reported as one exclusive video task so the cross-process gate does not load the LLM."""
        q = self._queue()
        if (q.get("running") or 0) + (q.get("pending") or 0) > 0:
            return [{"freevideo": {"class_type": "FreeVideoGenerate", "inputs": {}}}]
        return []

    def busy(self) -> bool:
        q = self._queue()
        return (q.get("running") or 0) + (q.get("pending") or 0) > 0

    def release(self) -> bool:
        return True
