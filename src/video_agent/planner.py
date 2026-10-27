"""LLM 规划器（可选、按需唤醒）：把自然语言翻译成受约束的数据。

- 只产出数据（动作、模板、参数、回复、记忆提议），一律再经模板槽位校验；不碰 ComfyUI 或 shell。
- 上下文按缓存友好顺序组装（S8，实测前缀命中 0.3–0.5 s vs 冷算约 48 s / 17k token）：
    system = 规则 + 模板说明（静态）+ 项目记忆（少变）+ 会话摘要（压缩时才变）
    history = 会话消息原文（只追加）
    final user = 实时状态（易变）+ 本次输入
- 会话变长时把较早的消息压缩进会话摘要（S4），压缩后预热。
- 后端可替换（S9）：本地 LM Studio、OpenAI 兼容接口；阶段 6 再加在线 agent。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import llm
from .backends import ChatResult, FuncBackend, LMStudioBackend, OpenAICompatBackend
from .config import ROOT, Settings
from .registry import Registry
from .render import TIER_KEYS, ParamError, resolve
from .store import ROUND_ZH, Store

MODES = {
    "on_demand": "按需唤醒（空闲自动卸载）",
    "session": "会话常驻（直到切回按需）",
    "always": "始终常驻",
    "command_only": "纯命令（不唤醒 LLM）",
}

_MEM = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["round", "select", "clarify", "reply"]},
        "template": {"type": "string"},
        "prompt": {"type": "string"},
        "image": {"type": "string"},
        "tier": {"type": "string"},
        "n": {"type": "integer"},
        "title": {"type": "string"},
        "round_ref": {"type": "string"},
        "pick": {"type": "integer"},
        "role": {"type": "string"},
        "memories": _MEM,
        "reply": {"type": "string"},
    },
    "required": [
        "action",
        "template",
        "prompt",
        "image",
        "title",
        "round_ref",
        "pick",
        "role",
        "memories",
        "reply",
    ],
}
DELTA_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "steps": {"type": "integer"},
        "memories": _MEM,
        "reply": {"type": "string"},
    },
    "required": ["prompt", "memories", "reply"],
}
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

RULES_FILE = ROOT / "prompts" / "planner_rules.md"  # 可直接编辑；改动会让前缀缓存失效一次


def load_rules() -> str:
    return RULES_FILE.read_text().strip()


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
    round_ref: str | None = None
    pick: int | None = None
    role: str | None = None
    memories: list[str] = field(default_factory=list)


def make_backend(cfg: Settings, store: Store):
    if cfg.planner_backend == "openai_compatible":
        return OpenAICompatBackend(cfg.openai_base_url, cfg.openai_model, cfg.openai_api_key)

    def ttl() -> int | None:
        mode = store.get_kv("llm_mode", "on_demand")
        return cfg.llm_session_ttl_s if mode == "on_demand" else None

    return LMStudioBackend(
        cfg.lms_url,
        cfg.llm_model,
        cfg.llm_context,
        ttl,
        allowed=lambda: store.get_kv("llm_mode", "on_demand") != "command_only",
    )


class Planner:
    def __init__(
        self,
        cfg: Settings,
        store: Store,
        registry: Registry,
        backend=None,
        chat: Callable[[list[dict], dict], str] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.backend = FuncBackend(chat) if chat else (backend or make_backend(cfg, store))

    # ---- 驻留 ----
    @property
    def mode(self) -> str:
        return self.store.get_kv("llm_mode", "on_demand")

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode}；可选：{', '.join(MODES)}")
        self.store.set_kv("llm_mode", mode)
        if not isinstance(self.backend, LMStudioBackend) or not llm.available():
            return
        if mode in ("session", "always"):
            self.backend.prepare()
        if mode == "command_only" and self.backend.is_loaded():
            llm.unload(self.cfg.llm_model)

    def available(self) -> bool:
        return self.mode != "command_only"

    # ---- 上下文组装 ----
    def _templates_text(self) -> str:
        lines = []
        for t in self.registry.templates.values():
            slots = ", ".join(
                f"{n}:{s.type}" + (f"[{s.min:g}..{s.max:g}]" if s.min is not None and s.max else "")
                for n, s in t.slots.items()
                if n not in TIER_KEYS
            )
            tiers = ", ".join(f"{k}{'' if v.verified else '(未实测)'}" for k, v in t.tiers.items())
            lines.append(f"- {t.name}（{t.kind}，{t.title}）参数：{slots}；档位：{tiers}")
        return "\n".join(lines)

    def system_prompt(self, project_id: str | None, session: dict | None) -> str:
        """稳定前缀：规则 + 模板（静态）→ 项目记忆 → 会话摘要。不放任何易变内容。"""
        parts = [load_rules(), "## 可用模板\n" + self._templates_text()]
        if project_id:
            p = self.store.get_project(project_id)
            mems = self.store.memories(project_id, "active")
            mem_text = "\n".join(f"- {m['text']}" for m in mems) or "（暂无）"
            parts.append(
                f"## 项目：{p['name']}\n### 项目记忆（跨会话共享，已由用户确认）\n{mem_text}"
            )
        if session:
            parts.append("### 本会话较早内容的摘要\n" + (session["summary"] or "（无）"))
        return "\n\n".join(parts)

    @staticmethod
    def _history_content(m: dict) -> tuple[str, str] | None:
        if m["channel"] == "llm":
            raw = m["meta"].get("raw") or {}
            act = {
                k: raw[k]
                for k in ("action", "template", "round_ref", "pick", "prompt")
                if raw.get(k)
            }
            text = m["text"] + (f"\n[动作] {json.dumps(act, ensure_ascii=False)}" if act else "")
            return "assistant", text
        if m["direction"] == "in" and m["channel"] in ("web", "telegram", "cli"):
            att = m["meta"].get("attachments") or []
            extra = "".join(f"\n[附图 @upload:{a['id']} {a.get('name', '')}]" for a in att)
            return "user", m["text"] + extra
        return None

    def history(self, session: dict | None, before_id: int | None) -> list[dict]:
        if not session:
            return []
        out: list[dict] = []
        for m in self.store.session_messages(session["id"], session["summary_upto"]):
            if before_id is not None and m["id"] >= before_id:
                break
            item = self._history_content(m)
            if item is None:
                continue
            role, text = item
            if out and out[-1]["role"] == role:
                out[-1]["content"] += "\n" + text
            else:
                out.append({"role": role, "content": text})
        if out and out[0]["role"] == "assistant":
            out.insert(0, {"role": "user", "content": "（接续之前的会话）"})
        return out

    def live_state(self, project_id: str | None) -> str:
        if not project_id:
            return "（无项目）"
        lines = []
        pack = self.store.pack(project_id)
        if pack:
            lines.append(
                f"基准包 v{pack['version']}："
                + "，".join(
                    f"@pack:{r}（{a['kind']}，来自任务 {a['task_id']}）"
                    for r, a in pack["assets"].items()
                )
            )
        else:
            lines.append("基准包：空")
        for r in self.store.rounds(project_id)[:4]:
            tasks = self.store.round_tasks(r["id"])
            cands = "，".join(f"#{t['idx']}=@task:{t['id']}" for t in tasks if t["outputs"])
            d = r["decision"] or {}
            dec = (
                f"；决定：{d.get('action')}"
                + (f" #{d['idx']}" if d.get("idx") else "")
                + (f"（{d['note']}）" if d.get("note") else "")
                if d.get("action")
                else ""
            )
            lines.append(
                f"- 轮 {r['id']}（{ROUND_ZH[r['state']]}，{r['template']}）"
                f"候选：{cands or '无'}{dec}\n  prompt: {r['base_params'].get('prompt', '')}"
            )
        return "\n".join(lines)

    def build(
        self,
        purpose_text: str,
        project_id: str | None,
        session_id: str | None,
        before_id: int | None,
    ) -> list[dict]:
        session = self.store.get_session(session_id) if session_id else None
        msgs = [{"role": "system", "content": self.system_prompt(project_id, session)}]
        msgs += self.history(session, before_id)
        final = f"## 当前状态（实时）\n{self.live_state(project_id)}\n\n## 本次输入\n{purpose_text}"
        if msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n\n" + final
        else:
            msgs.append({"role": "user", "content": final})
        return msgs

    # ---- 调用 ----
    def _call(
        self,
        purpose: str,
        messages: list[dict],
        schema: dict,
        project_id: str | None,
        session_id: str | None,
        log: bool = True,
    ) -> dict:
        res: ChatResult = self.backend.chat(messages, schema)
        data = _parse_json(res.text)
        if log:
            self.store.log_message(
                "llm",
                "out",
                data.get("reply", ""),
                project_id,
                getattr(self.backend, "model", self.backend.name),
                session_id=session_id,
                purpose=purpose,
                raw=data,
                backend=self.backend.name,
                **res.stats(),
            )
        self._last = res
        return data

    def plan_request(
        self,
        text: str,
        project_id: str | None,
        session_id: str | None = None,
        before_id: int | None = None,
        attachments: list[dict] | None = None,
    ) -> Plan:
        att = "".join(f"\n[附图 @upload:{a['id']} {a.get('name', '')}]" for a in attachments or [])
        msgs = self.build(f"用户：{text}{att}", project_id, session_id, before_id)
        data = self._call("plan", msgs, PLAN_SCHEMA, project_id, session_id)
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
            round_ref=data.get("round_ref") or None,
            pick=data.get("pick") or None,
            role=data.get("role") or None,
            memories=[m for m in data.get("memories") or [] if m.strip()][:3],
        )
        if plan.action == "round":
            self._validate(plan.template, plan.params, plan.tier)
        self.maybe_summarize(session_id)
        return plan

    def translate_note(
        self,
        template: str,
        base_params: dict,
        note: str,
        project_id: str | None,
        session_id: str | None = None,
        before_id: int | None = None,
    ) -> tuple[dict, str, list[str]]:
        """批注 → 参数增量（只允许模板槽位里的非尺寸、非图片参数）。"""
        t = self.registry.template(template)
        editable = [n for n, s in t.slots.items() if n not in TIER_KEYS and s.type != "image"]
        shown = {k: v for k, v in base_params.items() if k in editable}
        task = (
            f"把批注翻译成参数修改。模板 {template}，可改参数：{', '.join(editable)}\n"
            f"当前参数：{json.dumps(shown, ensure_ascii=False)}\n批注：{note}\n"
            "输出 prompt 为修改后的完整新 prompt（不需要改则为空字符串）；"
            "只有批注涉及细节程度时才给 steps。"
        )
        msgs = self.build(task, project_id, session_id, before_id)
        data = self._call("note", msgs, DELTA_SCHEMA, project_id, session_id)
        delta = {}
        if data.get("prompt"):
            delta["prompt"] = data["prompt"]
        if data.get("steps"):
            delta["steps"] = data["steps"]
        bad = set(delta) - set(editable)
        if bad:
            raise ParamError(f"规划器给出了不可改的参数：{', '.join(sorted(bad))}")
        self._validate(template, {**shown, **delta}, None, partial=True)
        self.maybe_summarize(session_id)
        mems = [m for m in data.get("memories") or [] if m.strip()][:3]
        return delta, data.get("reply", ""), mems

    # ---- 会话摘要与预热 ----
    def maybe_summarize(self, session_id: str | None, force: bool = False) -> bool:
        """上一次调用的 prompt 超过阈值时，把较早的消息压缩进会话摘要（唯一的前缀重写点）。"""
        last = getattr(self, "_last", None)
        if not session_id or (
            not force and (not last or last.prompt_tokens < self.cfg.session_summary_tokens)
        ):
            return False
        session = self.store.get_session(session_id)
        msgs = [
            m
            for m in self.store.session_messages(session_id, session["summary_upto"])
            if self._history_content(m)
        ]  # 只算进入 LLM 的消息（用户原话与规划器回复）
        keep = self.cfg.session_keep_messages
        if len(msgs) <= keep:
            return False
        old, upto = msgs[:-keep], msgs[-keep - 1]["id"]
        lines = []
        for m in old:
            role, text = self._history_content(m)
            lines.append(f"{'用户' if role == 'user' else '规划器'}：{text}")
        prompt = [
            {
                "role": "system",
                "content": "你是会话摘要器。把对话压缩成要点，保留：用户的要求与否定、"
                "选定 / 打回了哪些轮次与候选（含 ID）、仍未解决的问题。只输出 JSON。",
            },
            {
                "role": "user",
                "content": f"已有摘要：\n{session['summary'] or '（无）'}\n\n"
                f"需要并入的新对话：\n" + "\n".join(lines),
            },
        ]
        data = self._call(
            "summary", prompt, SUMMARY_SCHEMA, session["project_id"], session_id, log=False
        )
        self.store.update_session(session_id, summary=data.get("summary", ""), summary_upto=upto)
        self.store.log_message(
            "system",
            "out",
            f"会话较长，已把 {len(old)} 条较早消息压缩进摘要",
            session["project_id"],
            session_id=session_id,
        )
        self.prewarm(session_id)
        return True

    def prewarm(self, session_id: str | None) -> ChatResult | None:
        """把当前会话前缀写进缓存（模型重新加载或摘要重写后调用）。"""
        if not session_id or not self.available():
            return None
        s = self.store.get_session(session_id)
        if s is None:
            return None
        msgs = [{"role": "system", "content": self.system_prompt(s["project_id"], s)}]
        msgs += self.history(s, None) or [{"role": "user", "content": "（预热）"}]
        if msgs[-1]["role"] != "user":
            msgs.append({"role": "user", "content": "（预热）"})
        return self.backend.prewarm(msgs)

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
