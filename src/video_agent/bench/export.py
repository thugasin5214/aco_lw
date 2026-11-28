"""A finished bench run → the layout VAHBench scores: `<root>/aco/<brain>/<prompt_dir>/` with
events.jsonl (aco's agent messages in the OpenAI tool-call shape VAHBench reads as "hermes"),
run.json (target + status), verification.json (the episodes delivered) and out/episodeN.mp4.

What counts as delivered, in order: the files the agent sent with send_file; else the final cuts of
its productions; else the video outputs of the project, newest last.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..store import Store

VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def probe_seconds(path: Path) -> float | None:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return round(float(r.stdout.strip()), 2)
    except ValueError:
        return None


def project_runs(store: Store, project_id: str) -> list[dict]:
    return sorted(store.runs_of_project(project_id), key=lambda r: r["created"])


def trace_events(store: Store, project_id: str) -> list[dict]:
    """Every agent message of the project's runs, oldest first, as VAHBench "hermes" events."""
    out = []
    for run in project_runs(store, project_id):
        msgs = store.agent_msgs(run["id"])
        called = {c["id"] for m in msgs for c in (m["tool_calls"] or [])}
        for m in msgs:
            e = {"type": "message", "role": m["role"], "ts": m["created"], "run": run["id"]}
            if m["role"] == "assistant":
                e["content"] = m["content"] or ""
                if m["tool_calls"]:
                    e["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in m["tool_calls"]
                    ]
                stats = {k: m["meta"].get(k) for k in ("seconds", "prompt_tokens", "via")}
                e["meta"] = {k: v for k, v in stats.items() if v is not None}
            elif m["role"] == "tool":
                if m["tool_call_id"] not in called:
                    # a delegated CLI agent's call (over MCP) has no assistant turn of ours
                    out.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "ts": m["created"],
                            "run": run["id"],
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": m["tool_call_id"],
                                    "type": "function",
                                    "function": {"name": m["name"], "arguments": "{}"},
                                }
                            ],
                        }
                    )
                e["tool_call_id"] = m["tool_call_id"]
                e["name"] = m["name"]
                e["content"] = m["content"] or ""
            else:
                e["content"] = m["content"] if isinstance(m["content"], str) else ""
            out.append(e)
    return out


def delivered(store: Store, project_id: str) -> list[Path]:
    sent = []
    for run in project_runs(store, project_id):
        for m in store.agent_msgs(run["id"]):
            if m["role"] == "tool" and m["name"] == "send_file" and m["status"] == "done":
                try:
                    p = Path(json.loads(m["content"]).get("sent") or "")
                except (TypeError, ValueError, AttributeError):
                    continue
                if p.suffix.lower() in VIDEO_EXT and p.is_file() and p not in sent:
                    sent.append(p)
    if sent:
        return sent
    finals = []
    for prod in store.productions(project_id):
        for a in store.artifacts(prod["id"], "final", "current", "review"):
            tasks = [store.get(a["task_id"])] if a.get("task_id") else []
            if not tasks and a.get("round_id"):
                tasks = store.round_tasks(a["round_id"])
            for t in tasks:
                for o in (t or {}).get("outputs") or []:
                    p = store.task_dir(t["id"]) / o["file"]
                    if o["kind"] == "video" and p.is_file():
                        finals.append(p)
    if finals:
        return finals
    vids = []
    for r in store.rounds(project_id):
        for t in store.round_tasks(r["id"]):
            for o in t["outputs"] or []:
                p = store.task_dir(t["id"]) / o["file"]
                if o["kind"] == "video" and p.is_file():
                    vids.append((t["updated"], p))
    return [p for _, p in sorted(vids)]


def export(run_dir: Path, store: Store, root: Path) -> Path:
    info = json.loads((run_dir / "run.json").read_text())
    case = info["case"]
    folder = case.get("prompt_dir") or f"prompt_{case['id'].replace('-', '_')}"
    dest = root / "aco" / info["brain"] / folder
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "out").mkdir(parents=True)
    events = trace_events(store, info["project_id"])
    with (dest / "events.jsonl").open("w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    files = delivered(store, info["project_id"])
    target = case["target"]
    episodes = []
    for i, src in enumerate(files[: max(target["episodes"], len(files))], 1):
        out = dest / "out" / f"episode{i}{src.suffix.lower()}"
        shutil.copy2(src, out)
        episodes.append(
            {
                "episode": i,
                "file": f"out/{out.name}",
                "target": target["seconds_per_episode"],
                "seconds": probe_seconds(out),
                "source": str(src),
            }
        )
    runs = project_runs(store, info["project_id"])
    status = "completed" if runs and runs[-1]["status"] == "done" and episodes else "incomplete"
    (dest / "run.json").write_text(
        json.dumps(
            {
                "target": {
                    "episodes": target["episodes"],
                    "seconds_per_episode": target["seconds_per_episode"],
                    "aspect_ratio": target["aspect_ratio"],
                },
                "status": status,
                "harness": "aco",
                "model": info["brain"],
                "brief": case["brief"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (dest / "verification.json").write_text(
        json.dumps({"episodes": episodes}, ensure_ascii=False, indent=2)
    )
    (run_dir / "export.json").write_text(
        json.dumps({"dir": str(dest), "episodes": episodes}, ensure_ascii=False, indent=2)
    )
    return dest
