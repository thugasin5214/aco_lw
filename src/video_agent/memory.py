"""系统内存读数与生成期间的后台采样。

G0 结论：ComfyUI 进程 RSS 看不到 Metal 占用，门控与记录都以系统可用内存 + 压力等级为准。
"""

from __future__ import annotations

import subprocess
import threading
import time

import psutil

GIB = 2**30


def pressure_level() -> int:
    """kern.memorystatus_vm_pressure_level：1 正常，2 警告，4 严重；读不到返回 -1。"""
    out = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out) if out.isdigit() else -1


def snapshot() -> dict:
    vm = psutil.virtual_memory()
    return {
        "avail_gib": round(vm.available / GIB, 2),
        "used_gib": round(vm.used / GIB, 2),
        "swap_used_gib": round(psutil.swap_memory().used / GIB, 2),
        "pressure": pressure_level(),
    }


class Sampler:
    """`with Sampler() as s: ...` 结束后 `s.summary()` 给出峰值与 swap 变化。"""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.rows.append({"t": time.time(), **snapshot()})
            self._stop.wait(self.interval)

    def __enter__(self) -> Sampler:  # noqa: PYI034
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def summary(self) -> dict:
        if not self.rows:
            return {}
        first, last = self.rows[0], self.rows[-1]
        return {
            "min_avail_gib": min(r["avail_gib"] for r in self.rows),
            "peak_used_gib": max(r["used_gib"] for r in self.rows),
            "swap_delta_gib": round(last["swap_used_gib"] - first["swap_used_gib"], 2),
            "max_pressure": max(r["pressure"] for r in self.rows),
        }
