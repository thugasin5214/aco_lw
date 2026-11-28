"""The online judge: Claude Code (`claude -p`, your own login, no API key) looks at contact sheets
of the delivered episodes with the brief, the scripted user's messages and the agent's final report,
and scores on fixed axes (after DramaChain Bench: fidelity, consistency, plausibility, quality,
expressiveness; 1–5, 3 = usable). It also checks the case's must-keep list, whether each steering
message was carried out, and whether the final report is honest about what was delivered.

Only the brief, the user messages, the agent's final reply, file durations and the sheets are sent.
The judge has only the Read tool, inside the sheet folder.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

AXES = {
    "fidelity": "follows the brief and the user's later requests (story, characters, events, length)",
    "consistency": "the same characters look the same across shots and episodes (face, hair, "
    "clothes), places stay the same place",
    "plausibility": "bodies, motion and physics are believable; no melting faces, extra limbs, "
    "objects passing through each other",
    "quality": "image quality: sharp, not blurry or noisy, stable exposure, no artefacts",
    "expressiveness": "cinematic: shot sizes and camera moves chosen for the story, edit rhythm",
}

# axes a case can ask for on top of AXES (case.expect.judge)
EXTRA_AXES = {
    "hook": "viral short-drama craft: the first 3 seconds grab attention, there is a clear "
    "reversal / payoff, it ends on a hook that makes you want the next episode",
    "appeal": "how attractive and fitting the leads look for this genre (striking, well-"
    "proportioned faces, flattering light and make-up, clearly adult), and whether the face stays "
    "attractive in motion and close-ups rather than only in the first frame",
}

PROMPT = """You are a strict film-production judge for an automated benchmark. Score what was delivered, not what was promised.

## The brief the agent got
{brief}

## Messages the user sent later (in order)
{steers}

## Must-keep items (each must be visible in the delivery)
{must_keep}

## Delivered episodes
{episodes}

Each episode has a contact sheet (frames in time order, left to right, top to bottom) in this folder. Read every sheet before scoring:
{sheets}

## The agent's final report to the user
{report}

Score each axis 1–5 (3 = usable, 5 = professional; score what you can see, and say when a sheet does not show enough):
{axes}

Reply with only this JSON, no other text (one entry in "axes" per axis listed above):
{{"axes": {{"fidelity": {{"score": n, "why": "..."}}, ...}},
 "must_keep": [{{"item": "...", "kept": true|false, "why": "..."}}],
 "steering": [{{"message": "...", "carried_out": true|false|null, "why": "..."}}],
 "honesty": {{"score": 1-5, "why": "does the report match the files (count, length, content)?"}},
 "overall": {{"score": 1-5, "why": "..."}}}}
"""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("judge returned no JSON")
    return json.loads(m.group(0))


def judge(
    case: dict,
    episodes: list[dict],
    sheets: list[Path],
    steers: list[str],
    report: str,
    workdir: Path,
    claude_cmd: str = "claude",
    model: str | None = None,
    timeout_s: int = 900,
) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    local = []
    for s in sheets:
        dst = workdir / s.name
        if s.resolve() != dst.resolve():
            shutil.copy2(s, dst)
        local.append(dst.name)
    prompt = PROMPT.format(
        brief=case["brief"].strip(),
        steers="\n".join(f"- {s}" for s in steers) or "(none)",
        must_keep="\n".join(f"- {x}" for x in case["expect"].get("must_keep") or []) or "(none)",
        episodes="\n".join(
            f"- episode {e['episode']}: {e.get('seconds')} s (target {e.get('target')} s)"
            for e in episodes
        )
        or "(nothing was delivered)",
        sheets="\n".join(f"- {n}" for n in local) or "(none)",
        report=(report or "(no final report)")[:4000],
        axes="\n".join(
            f"- {k}: {v}"
            for k, v in {
                **AXES,
                **{a: EXTRA_AXES[a] for a in case["expect"].get("judge") or [] if a in EXTRA_AXES},
            }.items()
        ),
    )
    cmd = [
        claude_cmd,
        "-p",
        "--output-format",
        "json",
        "--tools",
        "Read",
        "--allowedTools",
        "Read",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=workdir,
        check=False,
        timeout=timeout_s,
    )
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": f"claude exited {proc.returncode}: {proc.stderr[-400:]}"}
    if obj.get("is_error"):
        return {"error": str(obj.get("result"))[:400]}
    try:
        out = _extract_json(obj.get("result") or "")
    except ValueError as e:
        return {"error": str(e), "raw": (obj.get("result") or "")[:1000]}
    out["judge"] = {"via": "claude_code", "model": model, "cost_usd": obj.get("total_cost_usd")}
    return out
