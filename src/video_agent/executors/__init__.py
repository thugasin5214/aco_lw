"""执行端接口（E1）：编排器只通过 Executor 与具体生成器打交道。

现在：ComfyExecutor（ComfyUI，FLUX.2 / FreeVideo）、FFmpegExecutor（剪辑合成）。以后：独立 LTX 服务、
其他本地生成器、在线生成服务——实现同一接口即可，编排器、调度器、恢复逻辑不变。

约定（与 vhotseat 一致的原则）：
- job_id 由编排器预先生成并落库，再 submit；崩溃后按 job_id 对账，永不自动重提。
- 只取消 / 释放自己的东西；`busy()` 把别人的任务也算在内（不抢占）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

PollState = Literal["queued", "running", "done", "failed", "lost"]


@dataclass
class Prepared:
    """提交前准备好的作业：执行端自己的载荷 + 要存进任务目录的副本。"""

    payload: Any
    uploads: dict = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)  # 文件名 → 文本内容
    note: str = ""


@dataclass
class Poll:
    state: PollState
    detail: str | None = None
    result: Any = None  # 执行端自己的完成记录，交给 collect


class Executor(Protocol):
    name: str

    def prepare(self, template, task: dict) -> Prepared:
        """渲染 + 校验 + 上传输入；失败抛异常（任务标为失败，不提交）。"""

    def submit(self, prepared: Prepared, job_id: str) -> None: ...

    def poll(self, job_id: str) -> Poll: ...

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        """下载产物到 dest，返回 [{file, kind, ...}]；可往 metrics 里补执行耗时等。"""

    def cancel(self, job_id: str) -> None:
        """只取消这个 job（自己的）。"""

    def queued_payloads(self) -> list[dict]:
        """执行端里正在跑 / 排队的全部任务（含别人提交的）的载荷，用于跨进程把关。"""

    def busy(self) -> bool:
        """执行端是否有任何任务（包括别人手动提交的）。"""

    def release(self) -> bool:
        """空闲时释放已加载的模型；忙则不动并返回 False。"""


def get_executors(cfg, comfy=None) -> dict[str, Executor]:
    from .comfy import ComfyExecutor
    from .ffmpeg import FFmpegExecutor

    return {"comfy": ComfyExecutor(comfy, cfg), "ffmpeg": FFmpegExecutor(cfg)}
