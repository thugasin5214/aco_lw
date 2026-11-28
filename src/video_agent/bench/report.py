"""One table over all scored runs: quality next to the time it cost."""

from __future__ import annotations

import json
from pathlib import Path


def _row(s: dict) -> dict:
    t, j = s["time"], s.get("judge") or {}
    axes = j.get("axes") or {}
    faces = (s.get("faces") or {}).get("episodes") or {}
    ref = [f.get("to_reference", {}).get("mean") for f in faces.values() if f.get("to_reference")]
    fq = [f["face_quality"] for f in faces.values() if f.get("face_quality")]
    delivered = sum(float(e.get("seconds") or 0) for e in s["delivered"])
    target = sum(float(e.get("target") or 0) for e in s["delivered"]) or None
    overall = (j.get("overall") or {}).get("score")
    hours = (t.get("wall_s") or 0) / 3600
    steer = [x["first_action_s"] for x in s["steering"] if x.get("first_action_s") is not None]
    vah = ((s.get("vahbench") or {}).get("rows") or [{}])[0]
    return {
        "case": s["case"],
        "brain": s["brain"],
        "episodes": len(s["delivered"]),
        "delivered_s": round(delivered, 1),
        "target_s": target,
        "overall": overall,
        **{k: (v or {}).get("score") for k, v in axes.items()},
        "honesty": (j.get("honesty") or {}).get("score"),
        "face_to_ref": round(sum(ref) / len(ref), 3) if ref else None,
        "face_px": round(sum(x["face_px"] for x in fq) / len(fq)) if fq else None,
        "face_sharpness": round(sum(x["sharpness"] for x in fq) / len(fq), 1) if fq else None,
        "wall_h": round(hours, 2),
        "llm_min": round(t["llm_s"] / 60, 1),
        "render_min": round(t["render_s"] / 60, 1),
        "render_s_per_s": t["render_s_per_delivered_s"],
        "wasted_min": round(t["wasted_render_s"] / 60, 1),
        "quality_per_hour": round(overall / hours, 2) if overall and hours else None,
        "max_pressure": t["max_pressure"],
        "turns": s["loops"]["llm_turns"],
        "refused_repeats": s["loops"]["refused_repeats"],
        "max_same_call": s["loops"]["max_identical_in_row"],
        "looks": s["self_review"]["looks"],
        "scaled_unchecked": s["self_review"]["scaled_without_check"],
        "never_ran": s["renders"]["never_ran"],
        "lora_mismatch": len(s["loras"]["base_mismatch"]),
        "lora_expected_hit": f"{len(s['loras']['expected_hit'])}/{len(s['loras']['expected'])}",
        "steer_action_s": round(sum(steer) / len(steer), 1) if steer else None,
        "vah_S": vah.get("S"),
        "vah_C": vah.get("C"),
        "vah_Q": vah.get("Q"),
        "vah_P": vah.get("P"),
    }


COLUMNS = [
    "case",
    "brain",
    "episodes",
    "delivered_s",
    "target_s",
    "overall",
    "fidelity",
    "consistency",
    "plausibility",
    "quality",
    "expressiveness",
    "hook",
    "appeal",
    "honesty",
    "face_to_ref",
    "face_px",
    "face_sharpness",
    "wall_h",
    "llm_min",
    "render_min",
    "render_s_per_s",
    "wasted_min",
    "quality_per_hour",
    "max_pressure",
    "turns",
    "refused_repeats",
    "max_same_call",
    "looks",
    "scaled_unchecked",
    "never_ran",
    "lora_mismatch",
    "lora_expected_hit",
    "steer_action_s",
    "vah_S",
    "vah_C",
    "vah_Q",
    "vah_P",
]


def build(runs_dir: Path) -> tuple[list[dict], str]:
    rows = []
    for f in sorted(runs_dir.glob("*/score.json")):
        try:
            rows.append(_row(json.loads(f.read_text())))
        except (KeyError, ValueError) as e:
            rows.append({"case": f.parent.name, "brain": f"(unreadable: {e})"})
    md = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        md.append(
            "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in COLUMNS) + " |"
        )
    return rows, "\n".join(md)
