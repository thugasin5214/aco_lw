"""T0.2 LLM 加载/卸载耗时：卸载 → 加载 → 首 token，重复 N 次；最后测 --ttl 空闲卸载。

结束时恢复到开始时的状态（若开始时已加载，则以相同 identifier / context 重新加载、无 TTL）。
    uv run python bench/llm_load.py --rounds 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx
from memsample import Sampler, snapshot

LMS_URL = "http://127.0.0.1:1234"


def lms(*args: str) -> str:
    return subprocess.run(["lms", *args], capture_output=True, text=True, check=True).stdout


def loaded(identifier: str) -> dict | None:
    return next((m for m in json.loads(lms("ps", "--json")) if m["identifier"] == identifier), None)


def timed(fn, *args) -> float:
    t = time.monotonic()
    fn(*args)
    return round(time.monotonic() - t, 2)


def load(model: str, ctx: int, ttl: int | None = None) -> None:
    args = ["load", model, "-y", "--identifier", model, "-c", str(ctx)]
    if ttl:
        args += ["--ttl", str(ttl)]
    lms(*args)


def first_token(model: str) -> float:
    """流式请求，返回到第一个内容块（含 reasoning）的秒数。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "回答一个字：好"}],
        "max_tokens": 8,
        "stream": True,
    }
    t = time.monotonic()
    with httpx.stream("POST", f"{LMS_URL}/v1/chat/completions", json=body, timeout=600) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and '"delta"' in line:
                return round(time.monotonic() - t, 2)
    return -1.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3.8-27b-splash")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--ttl", type=int, default=20)
    ap.add_argument("--out", type=Path, default=Path("data/bench/llm_load.json"))
    a = ap.parse_args()

    initial = loaded(a.model)
    ctx = initial["contextLength"] if initial else 131072
    result = {
        "model": a.model,
        "initially_loaded": bool(initial),
        "context_length": ctx,
        "quantization": initial and initial.get("quantization"),
        "size_bytes": initial and initial.get("sizeBytes"),
        "rounds": [],
    }
    with Sampler(a.out.with_suffix(".csv")) as s:
        if not initial:
            load(a.model, ctx)
        for i in range(a.rounds):
            before = snapshot(s.comfy_pid, [])
            unload_s = timed(lms, "unload", a.model)
            time.sleep(3)
            unloaded = snapshot(s.comfy_pid, [])
            load_s = timed(load, a.model, ctx)
            ttft_cold = first_token(a.model)
            ttft_warm = first_token(a.model)
            after = snapshot(s.comfy_pid, [])
            row = {
                "round": i + 1,
                "unload_s": unload_s,
                "load_s": load_s,
                "first_token_after_load_s": ttft_cold,
                "first_token_warm_s": ttft_warm,
                "avail_loaded_gib": round(before["avail_gib"], 2),
                "avail_unloaded_gib": round(unloaded["avail_gib"], 2),
                "avail_reloaded_gib": round(after["avail_gib"], 2),
                "freed_gib": round(unloaded["avail_gib"] - before["avail_gib"], 2),
            }
            print(row, flush=True)
            result["rounds"].append(row)

        # TTL：带 --ttl 重新加载，用一次后空等，看是否自动卸载
        lms("unload", a.model)
        load(a.model, ctx, ttl=a.ttl)
        first_token(a.model)
        time.sleep(a.ttl + 25)
        result["ttl"] = {"ttl_s": a.ttl, "auto_unloaded": loaded(a.model) is None}
        print(result["ttl"], flush=True)

        # 恢复初始状态
        if loaded(a.model):
            lms("unload", a.model)
        if initial:
            load(a.model, ctx)
    result["memory"] = s.summary()
    result["restored_loaded"] = loaded(a.model) is not None
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
