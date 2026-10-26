"""LLM 规划器（可选、按需唤醒）：把自然语言翻译成受约束的参数。

- 只产出数据（模板名、参数、参数增量、回复文字），一律再经模板槽位校验；不碰 ComfyUI 或 shell。
- 驻留模式：on_demand（默认，--ttl 空闲卸载）/ session / always / command_only（不唤醒）。
- 每次调用写入对话历史（channel=llm，含耗时），用于证明“审阅等待期间无 LLM 推理”。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from . import llm
from .config import Settings
from .registry import Registry
from .render import TIER_KEYS, ParamError, resolve
from .store import Store

MODES = {
    "on_demand": "按需唤醒（空闲自动卸载）",
    "session": "会话常驻（直到切回按需）",
    "always": "始终常驻",
    "command_only": "纯命令（不唤醒 LLM）",
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["round", "clarify", "reply"]},
        "template": {"type": "string"},
        "prompt": {"type": "string"},
        "image": {"type": "string"},
        "tier": {"type": "string"},
        "n": {"type": "integer"},
        "title": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "template", "prompt", "image", "title", "reply"],
}
DELTA_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "steps": {"type": "integer"},
        "reply": {"type": "string"},
    },
    "required": ["prompt", "reply"],
}

SYSTEM = """你是本地视频创作系统的规划器。你只输出 JSON，不执行任何操作。
规则：
- 只能使用下面列出的模板与参数名；尺寸、时长由档位（tier）决定，不要输出 width/height/seconds。
- 需要图片输入的模板（参考编辑、图生视频），image 写成 "@pack:角色名"（引用项目基准包）或 "@task:任务ID"；不需要时 image 为空字符串。
- prompt 必须是完整可用的生成提示词；action 不是 round 时 template、prompt、title 可为空字符串。
- prompt 用英文书写，沿用已有 prompt 的分段结构，只改需要改的部分。
- 候选数量不用问（不填 n 即用默认每轮数量）；能合理推断就直接 action="round"。
- 只有在无法判断“出图还是出视频”或视频缺少可用的首帧来源时，才用 action="clarify" 提一个问题。
- 风格信息不足时按项目已有 prompt 与基准包延续，不要追问。
- reply 用简体中文，一两句话，说明你打算怎么做。"""


class PlannerUnavailable(RuntimeError):
    pass


@dataclass
class Plan:
    action: str
    reply: str
    template: str | None = None
    params: dict = field(default_factory=dict)
    tier: str | None = None
    n: int | None = None
    title: str | None = None


class Planner:
    def __init__(
        self,
        cfg: Settings,
        store: Store,
        registry: Registry,
        chat: Callable[[list[dict], dict], str] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self._chat = chat or self._lmstudio_chat

    # ---- 驻留 ----
    @property
    def mode(self) -> str:
        return self.store.get_kv("llm_mode", "on_demand")

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode}；可选：{', '.join(MODES)}")
        self.store.set_kv("llm_mode", mode)
        if mode in ("session", "always") and llm.available():
            self.ensure_loaded()
        if mode == "command_only" and llm.available() and llm.loaded(self.cfg.llm_model):
            llm.unload(self.cfg.llm_model)

    def ensure_loaded(self) -> float:
        """返回本次加载耗时（已加载则为 0）。"""
        if self.mode == "command_only":
            raise PlannerUnavailable("当前是纯命令模式，不唤醒 LLM；用 /mode on_demand 切回")
        if llm.loaded(self.cfg.llm_model):
            return 0.0
        t = time.monotonic()
        ttl = self.cfg.llm_idle_ttl_s if self.mode == "on_demand" else None
        llm.load(self.cfg.llm_model, self.cfg.llm_context, ttl)
        return round(time.monotonic() - t, 1)

    # ---- 调用 ----
    def _lmstudio_chat(self, messages: list[dict], schema: dict) -> str:
        load_s = self.ensure_loaded()
        body = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4096,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "plan", "strict": True, "schema": schema},
            },
        }
        r = httpx.post(f"{self.cfg.lms_url}/v1/chat/completions", json=body, timeout=600)
        r.raise_for_status()
        self._last_load_s = load_s
        return r.json()["choices"][0]["message"]["content"]

    def _call(self, purpose: str, user: str, schema: dict, project_id: str | None) -> dict:
        self._last_load_s = 0.0
        t = time.monotonic()
        raw = self._chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], schema
        )
        dur = round(time.monotonic() - t, 1)
        data = _parse_json(raw)
        self.store.log_message(
            "llm",
            "out",
            data.get("reply", ""),
            project_id,
            self.cfg.llm_model,
            purpose=purpose,
            seconds=dur,
            load_s=self._last_load_s,
            raw=data,
        )
        return data

    # ---- 上下文 ----
    def _templates_text(self) -> str:
        lines = []
        for t in self.registry.templates.values():
            slots = ", ".join(
                f"{n}:{s.type}" + (f"[{s.min:g}..{s.max:g}]" if s.min is not None and s.max else "")
                for n, s in t.slots.items()
                if n not in TIER_KEYS
            )
            lines.append(
                f"- {t.name}（{t.kind}，{t.title}）参数：{slots}；档位：{', '.join(t.tiers)}"
            )
        return "\n".join(lines)

    def _project_text(self, project_id: str | None) -> str:
        if not project_id:
            return "（无项目上下文）"
        p = self.store.get_project(project_id)
        pack = self.store.pack(project_id)
        roles = (
            ", ".join(f"{r}（{a['kind']}）" for r, a in pack["assets"].items()) if pack else "空"
        )
        recent = []
        for r in self.store.rounds(project_id)[:3]:
            prompt = r["base_params"].get("prompt", "")
            recent.append(
                f"- 轮 {r['id']} 模板 {r['template']} 决定 {r['decision'].get('action')}"
                f"：prompt={prompt[:300]!r}"
            )
        return f"项目：{p['name']}\n基准包角色：{roles}\n最近几轮：\n" + "\n".join(
            recent or ["（无）"]
        )

    # ---- 对外 ----
    def plan_request(self, text: str, project_id: str | None) -> Plan:
        user = (
            f"可用模板：\n{self._templates_text()}\n\n{self._project_text(project_id)}\n\n"
            f"用户新需求：{text}"
        )
        data = self._call("plan", user, PLAN_SCHEMA, project_id)
        params = {"prompt": data["prompt"]} if data.get("prompt") else {}
        if data.get("image"):
            params["image"] = data["image"]
        plan = Plan(
            action=data.get("action", "reply"),
            reply=data.get("reply", ""),
            template=data.get("template") or None,
            params=params,
            tier=data.get("tier") or None,
            n=data.get("n") or None,
            title=data.get("title") or None,
        )
        if plan.action == "round":
            self._validate(plan.template, plan.params, plan.tier)
        return plan

    def translate_note(
        self, template: str, base_params: dict, note: str, project_id: str | None
    ) -> tuple[dict, str]:
        """批注 → 参数增量（只允许模板槽位里的非尺寸、非图片参数）。"""
        t = self.registry.template(template)
        editable = [n for n, s in t.slots.items() if n not in TIER_KEYS and s.type != "image"]
        shown = {k: v for k, v in base_params.items() if k in editable}
        user = (
            f"模板 {template} 可改参数：{', '.join(editable)}\n"
            f"当前参数：{json.dumps(shown, ensure_ascii=False)}\n"
            f"用户批注：{note}\n"
            "输出：prompt 为修改后的完整新 prompt（不需要改则为空字符串）；"
            "只有批注涉及细节程度时才给 steps。"
        )
        data = self._call("note", user, DELTA_SCHEMA, project_id)
        delta = dict(data.get("params_delta") or {})
        if data.get("prompt"):
            delta["prompt"] = data["prompt"]
        if data.get("steps"):
            delta["steps"] = data["steps"]
        bad = set(delta) - set(editable)
        if bad:
            raise ParamError(f"规划器给出了不可改的参数：{', '.join(sorted(bad))}")
        self._validate(template, {**shown, **delta}, None, partial=True)
        return delta, data.get("reply", "")

    def _validate(
        self, template: str | None, params: dict, tier: str | None, partial: bool = False
    ) -> None:
        if not template or template not in self.registry.templates:
            raise ParamError(f"规划器选了未知模板 {template}")
        t = self.registry.template(template)
        check = {k: v for k, v in params.items() if not (isinstance(v, str) and v.startswith("@"))}
        for name, s in t.slots.items():  # 引用类图片参数在创建任务时再解析
            if s.required and name not in check:
                check[name] = "placeholder"
        if partial:
            check = {k: v for k, v in check.items() if k in t.slots}
        resolve(t, check, tier, allow_unverified=True)


def _parse_json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            raise ParamError(f"规划器没有返回 JSON：{raw[:200]}") from None
        return json.loads(m.group(0))
