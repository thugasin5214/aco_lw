"""T0.3 ComfyUI 释放确认：队列两次确认为空后 /free，对比前后 ComfyUI RSS 与系统可用内存。

队列非空时拒绝，不取消任何任务。
    uv run python bench/comfy_free.py
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx
from memsample import Sampler, snapshot

COMFY = "http://127.0.0.1:8188"


def queue_busy(c: httpx.Client) -> bool:
    q = c.get("/queue").json()
    return bool(q["queue_running"] or q["queue_pending"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--settle", type=float, default=15, help="释放后观察秒数")
    ap.add_argument("--out", type=Path, default=Path("data/bench/comfy_free.json"))
    a = ap.parse_args()

    with httpx.Client(base_url=COMFY, timeout=30) as c, Sampler(a.out.with_suffix(".csv")) as s:
        if queue_busy(c):
            raise SystemExit("ComfyUI 队列非空，拒绝释放。")
        before = snapshot(s.comfy_pid, [])
        stats_before = c.get("/system_stats").json()["system"]["ram_free"] / 2**30
        time.sleep(1)
        if queue_busy(c):  # 二次确认，避开竞态
            raise SystemExit("ComfyUI 队列在确认间隙变为非空，拒绝释放。")
        t = time.monotonic()
        c.post("/free", json={"unload_models": True, "free_memory": True})
        api_s = round(time.monotonic() - t, 2)
        time.sleep(a.settle)
        after = snapshot(s.comfy_pid, [])
        stats_after = c.get("/system_stats").json()["system"]["ram_free"] / 2**30

    result = {
        "free_api_s": api_s,
        "comfy_rss_before_gib": round(before["comfy_rss_gib"], 2),
        "comfy_rss_after_gib": round(after["comfy_rss_gib"], 2),
        "sys_avail_before_gib": round(before["avail_gib"], 2),
        "sys_avail_after_gib": round(after["avail_gib"], 2),
        "comfy_reported_free_before_gib": round(stats_before, 2),
        "comfy_reported_free_after_gib": round(stats_after, 2),
        "memory": s.summary(),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
