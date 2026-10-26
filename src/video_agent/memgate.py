"""提交前的内存门控。

判据：系统可用内存 ≥ 模型实测峰值 + 余量，且压力等级未到严重，且 ComfyUI 队列空闲（不抢占手动任务）。
不足时依次：/free（仅队列空闲）→ 卸载 LLM → 等待；超时则放弃，不强行提交。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import llm
from .comfy import Comfy, ComfyBusy
from .config import Settings
from .memory import snapshot
from .registry import ModelInfo


@dataclass
class GateResult:
    ok: bool
    required_gib: float
    actions: list[str] = field(default_factory=list)
    unloaded_llm: bool = False
    last: dict = field(default_factory=dict)
    waited_s: float = 0.0


def ensure(
    comfy: Comfy,
    model: ModelInfo,
    cfg: Settings,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> GateResult:
    res = GateResult(ok=False, required_gib=model.peak_gib + cfg.memory_margin_gib)
    start = time.monotonic()
    freed = False

    if model.exclusive_llm and llm.loaded(cfg.llm_model):
        llm.unload(cfg.llm_model)
        res.unloaded_llm = True
        res.actions.append(f"卸载 LLM {cfg.llm_model}（该模型要求独占）")
        log(res.actions[-1])
        sleep(2)

    while True:
        busy = comfy.queue_busy()
        snap = snapshot()
        res.last = snap
        if not busy and snap["avail_gib"] >= res.required_gib and snap["pressure"] < 4:
            res.ok = True
            break
        if time.monotonic() - start > cfg.memory_wait_s:
            res.actions.append("等待超时")
            break
        if busy:
            log("ComfyUI 队列有其他任务，等待……")
        elif not freed:
            try:
                comfy.free()
                res.actions.append(f"/free（可用 {snap['avail_gib']} GiB < {res.required_gib}）")
                log(res.actions[-1])
            except ComfyBusy:
                pass
            freed = True
            sleep(3)
            continue
        elif not res.unloaded_llm and llm.loaded(cfg.llm_model):
            llm.unload(cfg.llm_model)
            res.unloaded_llm = True
            res.actions.append(f"卸载 LLM（可用 {snap['avail_gib']} GiB < {res.required_gib}）")
            log(res.actions[-1])
            sleep(2)
            continue
        sleep(10)
    res.waited_s = round(time.monotonic() - start, 1)
    return res
