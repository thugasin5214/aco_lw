"""T0.4 / T0.5 冒烟：提交一个 API 模板（可覆盖参数），轮询 /history，全程采样内存。

只在队列空闲时提交；超时不取消任务，只停止观察。
    uv run python bench/smoke.py TEMPLATE_API.json --set 20.value=1280 --set 21.value=720 --tag img-cold
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx
from memsample import Sampler

COMFY = "http://127.0.0.1:8188"


def parse_value(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template", type=Path)
    ap.add_argument("--set", action="append", default=[], metavar="NODE.FIELD=VALUE")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--outdir", type=Path, default=Path("data/bench"))
    a = ap.parse_args()

    graph = json.loads(a.template.read_text())
    for item in a.set:
        key, value = item.split("=", 1)
        node, field = key.split(".", 1)
        if field not in graph[node]["inputs"]:
            raise SystemExit(f"节点 {node} 没有字段 {field}")
        graph[node]["inputs"][field] = parse_value(value)

    out = a.outdir / a.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2))

    with httpx.Client(base_url=COMFY, timeout=30) as c, Sampler(out / "mem.csv") as s:
        q = c.get("/queue").json()
        if q["queue_running"] or q["queue_pending"]:
            raise SystemExit("ComfyUI 队列非空，不提交。")
        t0 = time.monotonic()
        r = c.post("/prompt", json={"prompt": graph, "client_id": str(uuid.uuid4())})
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        (out / "prompt_id.txt").write_text(prompt_id)
        print("submitted", prompt_id, flush=True)
        entry = None
        while time.monotonic() - t0 < a.timeout:
            h = c.get(f"/history/{prompt_id}").json()
            if prompt_id in h and h[prompt_id].get("status", {}).get("completed") is not None:
                entry = h[prompt_id]
                if entry["status"].get("status_str") in ("success", "error"):
                    break
            time.sleep(2)
        wall_s = round(time.monotonic() - t0, 1)

    status = entry["status"] if entry else {"status_str": "timeout"}
    msgs = {m[0]: m[1].get("timestamp") for m in status.get("messages", [])}
    exec_s = None
    if "execution_start" in msgs and ("execution_success" in msgs or "execution_error" in msgs):
        end = msgs.get("execution_success") or msgs.get("execution_error")
        exec_s = round((end - msgs["execution_start"]) / 1000, 1)
    result = {
        "tag": a.tag,
        "template": str(a.template),
        "overrides": a.set,
        "prompt_id": prompt_id,
        "status": status.get("status_str"),
        "wall_s": wall_s,
        "exec_s": exec_s,
        "outputs": entry.get("outputs") if entry else None,
        "memory": s.summary(),
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "outputs"}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
