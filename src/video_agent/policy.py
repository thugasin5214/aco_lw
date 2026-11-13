"""Policy layer (P1 continuation policy + P2 execution policy): read from policies.yaml; the runtime only follows it.

- Continuation: when an async result arrives, decide between handing it back to the LLM, auto next step, or bounded retry; anything unlisted goes back to the LLM.
- Execution: whether each tool is allowed, parameter limits, when your approval is needed; network access is off by default.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .config import ROOT

OOM_MARKERS = ("out of memory", "oom", "mps backend out of memory", "cannot allocate")


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Policies:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, data_dir: Path | None = None) -> Policies:
        base = yaml.safe_load((ROOT / "policies.yaml").read_text())
        user = data_dir / "policies.yaml" if data_dir else None
        if user and user.exists():
            base = _merge(base, yaml.safe_load(user.read_text()) or {})
        return cls(base)

    # ---- execution policy ----
    def tool(self, name: str) -> dict:
        return self.data["execution"]["tools"].get(name, {"allow": False})

    def check(self, name: str, args: dict) -> str | None:
        """Return the reason for refusing; None means allowed. Only refuses, never rewrites parameters (that would change what the task means)."""
        rule = self.tool(name)
        if not rule.get("allow"):
            return f"tool {name} is not permitted by the execution policy"
        if "max_n" in rule and (args.get("n") or 0) > rule["max_n"]:
            return f"n={args['n']} exceeds the policy limit of {rule['max_n']}"
        if "max_steps" in rule and len(args.get("steps") or []) > rule["max_steps"]:
            return f"{len(args['steps'])} steps exceed the policy limit of {rule['max_steps']}"
        return None

    def approval(self, name: str, key: str = "approval") -> str:
        return self.tool(name).get(key, "never")

    # ---- continuation policy ----
    def on_round(self, outcome: str, retries_done: int, in_pipeline: bool, last_step: bool) -> str:
        """outcome: succeeded | failed:oom | failed:lost | failed:other
        Returns: resume_llm | retry | auto_next | finished"""
        c = self.data["continuation"]
        if outcome == "succeeded":
            if in_pipeline:
                return "finished" if last_step else c.get("pipeline_step_succeeded", "resume_llm")
            return c.get("round_succeeded", "resume_llm")
        kind = outcome.split(":", 1)[1]
        rule = c["round_failed"].get(kind, c["round_failed"].get("other", {}))
        if rule.get("action") == "retry" and retries_done < rule.get("max_retries", 0):
            return "retry"
        return c.get("pipeline_step_failed", "resume_llm") if in_pipeline else "resume_llm"


def classify_round(store, round_id: str) -> str:
    tasks = store.round_tasks(round_id)
    if any(t["outputs"] for t in tasks):
        return "succeeded"
    errors = " ".join((t["error"] or "") for t in tasks).lower()
    if any(m in errors for m in OOM_MARKERS):
        return "failed:oom"
    if "lost" in errors:
        return "failed:lost"
    return "failed:other"
