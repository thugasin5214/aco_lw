"""Executor interface (E1): the orchestrator deals with concrete generators only through Executor.

Now: ComfyExecutor (ComfyUI, FLUX.2 / FreeVideo), FFmpegExecutor (editing and compositing). Later: a standalone LTX service,
other local generators, online generation services — implement the same interface; orchestrator, scheduler and recovery logic stay the same.

Conventions (same principles as vhotseat):
- job_id is generated and stored by the orchestrator before submit; after a crash reconcile by job_id, never resubmit automatically.
- Only cancel / release our own things; `busy()` counts other people's tasks too (no preemption).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

PollState = Literal["queued", "running", "done", "failed", "lost"]


@dataclass
class Prepared:
    """A job prepared before submitting: the executor's own payload + copies to store in the task directory."""

    payload: Any
    uploads: dict = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)  # file name → text content
    note: str = ""


@dataclass
class Poll:
    state: PollState
    detail: str | None = None
    result: Any = None  # the executor's own completion record, passed to collect
    # the executor confirms the job is still progressing (e.g. recent heartbeat): keep watching past
    # the watch limit, do not mark timeout
    alive: bool = False


class Executor(Protocol):
    name: str

    def prepare(self, template, task: dict) -> Prepared:
        """Render + validate + upload inputs; raise on failure (the task is marked failed, nothing submitted)."""

    def submit(self, prepared: Prepared, job_id: str) -> None: ...

    def poll(self, job_id: str) -> Poll: ...

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        """Download outputs to dest, return [{file, kind, ...}]; may add execution time etc. to metrics."""

    def cancel(self, job_id: str) -> None:
        """Cancel only this job (our own)."""

    def queued_payloads(self) -> list[dict]:
        """Payloads of every task running / queued in the executor (including other people's), for cross-process gating."""

    def busy(self) -> bool:
        """Whether the executor has any task at all (including ones others submitted by hand)."""

    def release(self) -> bool:
        """Release loaded models when idle; when busy, leave them and return False."""


def get_executors(cfg, comfy=None) -> dict[str, Executor]:
    from ..freevideo import FreeVideo
    from .comfy import ComfyExecutor
    from .ffmpeg import FFmpegExecutor
    from .freevideo import FreeVideoExecutor

    out = {"comfy": ComfyExecutor(comfy, cfg), "ffmpeg": FFmpegExecutor(cfg)}
    if getattr(cfg, "freevideo_agent", True):
        out["freevideo"] = FreeVideoExecutor(
            FreeVideo(cfg.comfy_url), cfg.data_dir / "freevideo_jobs.json"
        )
    return out
