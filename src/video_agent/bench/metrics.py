"""aco's own measurements of a bench run, all from its database and the runner's timeline (no
model involved). Every number here is measured.

- time: wall, LLM, render (and per delivered second), gate waits, wasted render time
- loops: tool calls, refused repeats, identical calls in a row, status polling, failed calls
- self-review: `look` calls; whether a render was scaled up without a cheaper draft before it
- LoRAs: which were used, on which model family, whether the base fits, expected ones hit
- failed submissions: renders that never ran (graph / validation errors)
- steering: for every scripted message, how long until the agent's next turn and next action
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from ..store import Store
from .export import probe_seconds, project_runs

READ_TOOLS = {
    "get_status",
    "get_project",
    "get_round",
    "get_production_plan",
    "list_templates",
    "list_workflows",
    "list_comfy_workflows",
    "get_comfy_workflow",
    "comfy_catalog",
}
WAIT_TOOLS = {"wait_for_round", "wait_for_stage", "wait_for_production"}
REPEAT_MARK = "you made this exact call"
WASTED_STATES = {"failed", "timeout", "rejected"}


def _json(s) -> dict:
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def family_of(graph_or_template: dict | str) -> str:
    """minimax-h3 / flux2-klein-4b / z-image / other, from a graph's node types or a template."""
    text = json.dumps(graph_or_template).lower()
    if "minimaxh3" in text or "minimax_h3" in text or "freevideo" in text or "h3-" in text:
        return "minimax-h3"
    if "klein" in text or "flux2" in text:
        return "flux2-klein-4b"
    if "z_image" in text or "zimage" in text:
        return "z-image"
    return "other"


def render_cost(task: dict) -> float:
    """Pixels × frames of a render (relative cost, for draft-vs-scale-up)."""
    p = task["params"] or {}
    graph = _json(p.get("graph")) if p.get("graph") else {}
    w = h = frames = None
    for n in graph.values():
        i = n.get("inputs") or {}
        w = w or (i.get("width") if isinstance(i.get("width"), int) else None)
        h = h or (i.get("height") if isinstance(i.get("height"), int) else None)
        for k in ("length", "frames", "num_frames"):
            if isinstance(i.get(k), int):
                frames = frames or i[k]
    w = w or p.get("width") or 1
    h = h or p.get("height") or 1
    frames = frames or p.get("length") or p.get("frames") or (24 * (p.get("seconds") or 1))
    steps = next(
        (
            n["inputs"]["steps"]
            for n in graph.values()
            if isinstance(n.get("inputs", {}).get("steps"), int)
        ),
        p.get("steps") or 1,
    )
    try:
        return float(w) * float(h) * float(frames) * float(steps)
    except (TypeError, ValueError):
        return 0.0


def loras_of(task: dict) -> list[dict]:
    p = task["params"] or {}
    out = []
    graph = _json(p.get("graph")) if p.get("graph") else {}
    fam = family_of(graph or task["template"])
    for n in graph.values():
        i = n.get("inputs") or {}
        if isinstance(i.get("lora_name"), str):
            out.append({"lora": i["lora_name"], "strength": i.get("strength_model"), "family": fam})
    for k in ("lora", "lora1", "lora2", "lora3"):
        if isinstance(p.get(k), str) and p[k]:
            out.append({"lora": p[k], "strength": p.get(f"{k}_strength"), "family": fam})
    return out


def collect(run_dir: Path, store: Store, lora_bases: dict[str, str] | None = None) -> dict:
    info = json.loads((run_dir / "run.json").read_text())
    case = info["case"]
    pid = info["project_id"]
    timeline = [
        json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines() if x.strip()
    ]
    runs = project_runs(store, pid)
    msgs = [m for r in runs for m in store.agent_msgs(r["id"])]
    tasks = [t for r in store.rounds(pid) for t in store.round_tasks(r["id"])]
    rounds = {r["id"]: r for r in store.rounds(pid)}

    # ---- time ----
    llm_s = sum(float(m["meta"].get("seconds") or 0) for m in msgs if m["role"] == "assistant")
    for prod in store.productions(pid):
        for a in store.artifacts(prod["id"]):
            llm_s += float(((a["meta"] or {}).get("llm") or {}).get("seconds") or 0)
    exec_s = {t["id"]: float((t["metrics"] or {}).get("exec_s") or 0) for t in tasks}
    gate_wait = sum(
        float(((t["metrics"] or {}).get("gate") or {}).get("waited_s") or 0) for t in tasks
    )
    render_s = sum(exec_s.values())
    wasted = 0.0
    for t in tasks:
        rnd = rounds.get(t["round_id"]) or {}
        rejected = (rnd.get("decision") or {}).get("action") in ("reject", "cancel")
        if t["state"] in WASTED_STATES or (rejected and t["outputs"]):
            wasted += exec_s[t["id"]]
    exp = _json((run_dir / "export.json").read_text()) if (run_dir / "export.json").exists() else {}
    delivered_s = sum(float(e.get("seconds") or 0) for e in exp.get("episodes") or [])
    mem = [(t["metrics"] or {}).get("memory") or {} for t in tasks]

    # ---- loops / calls ----
    calls = [
        (c["name"], c["arguments"])
        for m in msgs
        if m["role"] == "assistant"
        for c in (m["tool_calls"] or [])
    ]
    results = [m for m in msgs if m["role"] == "tool"]
    same_in_row, best = 1, 1
    for a, b in itertools.pairwise(calls):
        same_in_row = same_in_row + 1 if a == b else 1
        best = max(best, same_in_row)
    errors = sum(1 for m in results if '"error"' in (m["content"] or "")[:200])

    # ---- self-review: scaled up without a cheaper draft (and a look) before it ----
    renders = sorted([t for t in tasks if t["kind"] == "video"], key=lambda t: t["created"])
    looks = sorted(
        m["created"]
        for m in msgs
        if m["role"] == "assistant"
        for c in (m["tool_calls"] or [])
        if c["name"] == "look"
    )
    # a render is "scaled up" when it costs over 2x the cheapest earlier one; it was checked when a
    # look (the agent) or a pick (the user) came between that cheaper render and it. A costly render
    # with nothing cheaper before it went straight to full size without a draft.
    picks = sorted(e["ts"] for e in timeline if e["kind"] in ("pick", "review"))
    low_all = min((render_cost(t) for t in renders), default=0)
    unchecked = no_draft = 0
    cheapest: dict | None = None
    first_scaled = None
    for t in renders:
        cost = render_cost(t)
        if cheapest is not None and cost > 2 * render_cost(cheapest):
            first_scaled = first_scaled or t
            seen = [x for x in (*looks, *picks) if cheapest["created"] < x < t["created"]]
            unchecked += not seen
        elif cheapest is None and len(renders) > 1 and cost > 2 * low_all:
            no_draft += 1
        if cheapest is None or cost < render_cost(cheapest):
            cheapest = t
    if len(renders) == 1:
        no_draft = 1  # one render only: nothing was tried smaller first
    # ---- LoRAs ----
    used = [
        x for t in tasks if t["state"] not in ("failed",) or exec_s[t["id"]] for x in loras_of(t)
    ]
    bases = lora_bases or {}
    mismatch = [u for u in used if bases.get(u["lora"]) and bases[u["lora"]] != u["family"]]
    expected = case["expect"].get("loras") or []
    hit = [e for e in expected if any(e.lower() in u["lora"].lower() for u in used)]

    # ---- failed submissions (never ran) ----
    never_ran = [t for t in tasks if t["state"] == "failed" and not exec_s[t["id"]]]

    # ---- steering ----
    steer = []
    for ev in timeline:
        if ev["kind"] != "user" or not ev.get("why", "").startswith("steer"):
            continue
        after = [m for m in msgs if m["created"] > ev["ts"]]
        turn = next((m for m in after if m["role"] == "assistant"), None)
        act = next(
            (
                m
                for m in after
                if m["role"] == "assistant"
                and m["tool_calls"]
                and any(c["name"] not in READ_TOOLS | WAIT_TOOLS for c in m["tool_calls"])
            ),
            None,
        )
        steer.append(
            {
                "say": ev["text"],
                "at_s": ev["t"],
                "first_turn_s": round(turn["created"] - ev["ts"], 1) if turn else None,
                "first_action_s": round(act["created"] - ev["ts"], 1) if act else None,
            }
        )

    final_reply = next(
        (
            m["content"]
            for m in reversed(msgs)
            if m["role"] == "assistant" and not m["tool_calls"] and m["content"]
        ),
        "",
    )
    return {
        "case": case["id"],
        "brain": info["brain"],
        "time": {
            "wall_s": info.get("wall_s"),
            "llm_s": round(llm_s, 1),
            "render_s": round(render_s, 1),
            "gate_wait_s": round(gate_wait, 1),
            "wasted_render_s": round(wasted, 1),
            "delivered_s": round(delivered_s, 1),
            "render_s_per_delivered_s": round(render_s / delivered_s, 1) if delivered_s else None,
            "peak_used_gib": max((m.get("peak_used_gib") or 0 for m in mem), default=0),
            "max_pressure": max((m.get("max_pressure") or 0 for m in mem), default=0),
            "swap_delta_gib": max((m.get("swap_delta_gib") or 0 for m in mem), default=0),
        },
        "loops": {
            "runs": len(runs),
            "llm_turns": sum(1 for m in msgs if m["role"] == "assistant"),
            "tool_calls": len(calls),
            "refused_repeats": sum(1 for m in results if REPEAT_MARK in (m["content"] or "")),
            "max_identical_in_row": best if calls else 0,
            "status_reads": sum(1 for n, _ in calls if n in READ_TOOLS),
            "waits": sum(1 for n, _ in calls if n in WAIT_TOOLS),
            "tool_errors": errors,
        },
        "self_review": {
            "looks": len(looks),
            "renders": len(renders),
            "scaled_without_check": unchecked,
            "no_draft_first": no_draft,
            "first_scale_up_after_s": (
                round(first_scaled["created"] - renders[0]["created"], 1) if first_scaled else None
            ),
        },
        "renders": {
            "tasks": len(tasks),
            "never_ran": len(never_ran),
            "never_ran_errors": sorted({(t["error"] or "")[:120] for t in never_ran})[:5],
            "templates": sorted({t["template"] for t in tasks}),
        },
        "loras": {
            "used": used,
            "base_mismatch": mismatch,
            "expected": expected,
            "expected_hit": hit,
        },
        "steering": steer,
        "final_reply": final_reply[:4000],
        "delivered": exp.get("episodes") or [],
    }


def delivered_seconds(paths: list[Path]) -> float:
    return sum(probe_seconds(p) or 0 for p in paths)
