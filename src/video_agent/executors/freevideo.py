"""FreeVideo 执行端（agent API）。

- `freevideo-take`：电影的 take 由 FreeVideo 自己排队拍摄（见 production.py 的 film 阶段），这里不提交，
  只让资源闸门看得见 FreeVideo 自己的队列（拍片时不加载 LLM、不抢内存）。
- 其他模板（如 `freevideo-ref`）：组成一个 `POST /jobs` 的 generate 作业——参考视频（可只引导前 K 步）、
  参考图、首帧——由编排器提交、轮询、下载。编排器的 job_id 作为幂等键 `ref`，FreeVideo 的作业 ID
  记在 `jobs_file` 里，daemon 重启后仍能对账（永不自动重提）。
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
    """近 HEARTBEAT_FRESH_S 秒内有心跳 = 作业还在推进。"""
    hb = job.get("last_heartbeat_at")
    if not hb:
        return False
    try:
        ts = datetime.fromisoformat(hb).timestamp()
    except ValueError:
        return False
    return (now if now is not None else time.time()) - ts < HEARTBEAT_FRESH_S


def build_job(params: dict, media: dict[str, str], platform: str = sys.platform) -> dict:
    """参数 + 已上传的 media 名 → generate 作业（纯函数，便于测试）。
    references 的顺序决定提示词里的编号：<Video 1> 是参考视频，<Picture 1>/<Picture 2> 依次是参考图。"""
    q = params.get("quality", "light")
    if q not in QUALITIES:
        raise ValueError(f"quality 只能是 {'/'.join(QUALITIES)}，收到 {q}")
    refs: list = []
    if params.get("early_steps") and platform == "darwin":
        # FreeVideo 的 Mac 编码路径没实现 early_steps：会先编码几个小时再报
        # "early_steps requires separately encoded early reference conditions"（实测 2026-11-07）
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

    # ---- 编排器 job_id → FreeVideo 作业 ID（落盘，重启后对账） ----
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
        except Exception:  # noqa: BLE001 — FreeVideo 不可达：不当作忙
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
        except Exception as e:  # noqa: BLE001 — 404 = FreeVideo 里没有；其他（网络抖动）下次再问
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
        if fv_id:  # 只取消自己提交的这一个
            self.client.call("POST", f"/jobs/{fv_id}/cancel", {})

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
