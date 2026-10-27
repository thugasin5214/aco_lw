"""策略层（P1 续跑策略 + P2 执行策略）：从 policies.yaml 读取，运行时只按它行事。

- 续跑策略：异步结果到来时，决定交回 LLM、自动下一步、有限重试；没写到的一律交回 LLM。
- 执行策略：每个工具是否允许、参数上限、何时需要你批准；网络访问默认关闭。
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

    # ---- 执行策略 ----
    def tool(self, name: str) -> dict:
        return self.data["execution"]["tools"].get(name, {"allow": False})

    def check(self, name: str, args: dict) -> str | None:
        """返回拒绝原因；None 表示允许。只拒绝，不改参数（不改变任务含义）。"""
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

    # ---- 续跑策略 ----
    def on_round(self, outcome: str, retries_done: int, in_pipeline: bool, last_step: bool) -> str:
        """outcome: succeeded | failed:oom | failed:lost | failed:other
        返回：resume_llm | retry | auto_next | finished"""
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
