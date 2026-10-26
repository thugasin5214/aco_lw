"""T0.1 内存采样器：后台线程定时采样系统内存与 ComfyUI / LM Studio 进程占用。

可作为库使用（`with Sampler(path) as s: ...; s.summary()`），也可单独运行：
    uv run python bench/memsample.py --seconds 30 --out data/bench/mem.csv
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import threading
import time
from pathlib import Path
from typing import Self

import psutil

GIB = 2**30
FIELDS = [
    "t",
    "avail_gib",
    "used_gib",
    "wired_gib",
    "swap_used_gib",
    "pressure",
    "comfy_rss_gib",
    "lms_rss_gib",
]


def pid_on_port(port: int) -> int | None:
    out = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], check=False, capture_output=True, text=True
    ).stdout.split()
    return int(out[0]) if out else None


def pressure_level() -> int:
    """kern.memorystatus_vm_pressure_level：1 正常，2 警告，4 严重。"""
    out = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out) if out.isdigit() else -1


def _tree_rss(pid: int | None) -> float:
    if pid is None:
        return 0.0
    try:
        p = psutil.Process(pid)
        procs = [p, *p.children(recursive=True)]
    except psutil.NoSuchProcess:
        return 0.0
    total = 0
    for q in procs:
        try:
            total += q.memory_info().rss
        except psutil.Error:
            pass
    return total / GIB


def _lms_pids() -> list[int]:
    pids = []
    for p in psutil.process_iter(["name", "exe"]):
        exe = p.info.get("exe") or ""
        if "LM Studio" in exe or ".lmstudio" in exe:
            pids.append(p.pid)
    return pids


def snapshot(comfy_pid: int | None, lms_pids: list[int]) -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    lms = 0.0
    for pid in lms_pids:
        try:
            lms += psutil.Process(pid).memory_info().rss / GIB
        except psutil.Error:
            pass
    return {
        "t": time.time(),
        "avail_gib": vm.available / GIB,
        "used_gib": vm.used / GIB,
        "wired_gib": getattr(vm, "wired", 0) / GIB,
        "swap_used_gib": sw.used / GIB,
        "pressure": pressure_level(),
        "comfy_rss_gib": _tree_rss(comfy_pid),
        "lms_rss_gib": lms,
    }


class Sampler:
    def __init__(self, out: Path, interval: float = 0.5, comfy_port: int = 8188):
        self.out = out
        self.interval = interval
        self.comfy_pid = pid_on_port(comfy_port)
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        lms_pids = _lms_pids()
        while not self._stop.is_set():
            self.rows.append(snapshot(self.comfy_pid, lms_pids))
            self._stop.wait(self.interval)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(self.rows)

    def mark(self) -> dict:
        return snapshot(self.comfy_pid, _lms_pids())

    def summary(self) -> dict:
        if not self.rows:
            return {}
        first, last = self.rows[0], self.rows[-1]
        return {
            "samples": len(self.rows),
            "duration_s": round(last["t"] - first["t"], 1),
            "min_avail_gib": round(min(r["avail_gib"] for r in self.rows), 2),
            "peak_used_gib": round(max(r["used_gib"] for r in self.rows), 2),
            "peak_wired_gib": round(max(r["wired_gib"] for r in self.rows), 2),
            "peak_comfy_rss_gib": round(max(r["comfy_rss_gib"] for r in self.rows), 2),
            "peak_lms_rss_gib": round(max(r["lms_rss_gib"] for r in self.rows), 2),
            "swap_delta_gib": round(last["swap_used_gib"] - first["swap_used_gib"], 2),
            "max_pressure": max(r["pressure"] for r in self.rows),
        }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", type=Path, default=Path("data/bench/mem.csv"))
    a = ap.parse_args()
    with Sampler(a.out, a.interval) as s:
        time.sleep(a.seconds)
    print(s.summary())
