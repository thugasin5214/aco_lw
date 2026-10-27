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

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import llm
from .backends import ChatResult, FuncBackend, LMStudioBackend, OpenAICompatBackend
from .config import ROOT, Settings
from .i18n import tr
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


API_BACKENDS = ("lmstudio", "openai_compatible", "anthropic")
AGENT_CLIS = ("claude_code", "codex")


def make_named_backend(cfg: Settings, store: Store, name: str):
    """按名字建后端：lmstudio（本地）、openai_compatible、anthropic（在线）。"""
    if name == "openai_compatible":
        return OpenAICompatBackend(cfg.openai_base_url, cfg.openai_model, cfg.openai_api_key)
    if name == "anthropic":
        from .backends import AnthropicBackend

        return AnthropicBackend(
            cfg.anthropic_model,
            cfg.anthropic_effort,
            cfg.anthropic_api_key or None,
            cfg.anthropic_fallbacks,
        )
    return make_backend(cfg, store)


def make_backend(cfg: Settings, store: Store):
    if cfg.planner_backend in ("openai_compatible", "anthropic"):
        return make_named_backend(cfg, store, cfg.planner_backend)

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
        self._resources = None

    @property
    def resources(self):
        return self._resources

    @resources.setter
    def resources(self, rm) -> None:
        """注入 ResourceManager，并让本地后端每次加载模型前都先问它（跨进程把关）。"""
        self._resources = rm
        if rm is not None and hasattr(self.backend, "load_guard"):
            self.backend.load_guard = lambda: None if rm.llm_admit()[0] else rm.llm_admit()[1]

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
        for t in self.registry.builtin_and_user().values():
            slots = ", ".join(
                f"{n}:{s.type}" + (f"[{s.min:g}..{s.max:g}]" if s.min is not None and s.max else "")
                for n, s in t.slots.items()
                if n not in TIER_KEYS
            )
            tiers = ", ".join(f"{k}{'' if v.verified else '(未实测)'}" for k, v in t.tiers.items())
            lines.append(f"- {t.name}（{t.kind}，{t.title}）参数：{slots}；档位：{tiers}")
        return "\n".join(lines)

    def system_prompt(
        self, project_id: str | None, session: dict | None, rules: str | None = None
    ) -> str:
        """稳定前缀：规则 + 模板（静态）→ 项目记忆 → 会话摘要。不放任何易变内容。"""
        parts = [rules or load_rules(), "## 可用模板\n" + self._templates_text()]
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
    def _assistant_text(m: dict) -> str:
        raw = m["meta"].get("raw") or {}
        act = {
            k: raw[k] for k in ("action", "template", "round_ref", "pick", "prompt") if raw.get(k)
        }
        return m["text"] + (f"\n[动作] {json.dumps(act, ensure_ascii=False)}" if act else "")

    def _pairs(self, session: dict, before_id: int | None = None) -> list[tuple[dict, dict]]:
        """会话里每次规划调用的 (当时发出的 user 原文, 规划器回复) 对。

        实测（docs/baseline.md）：该模型的前缀缓存只能“延长”，不能截断复用，
        所以历史必须逐字复现当时发出的内容，下一次请求才是上一次的延长。
        """
        pairs, last_user = [], "（接续之前的会话）"
        for m in self.store.session_messages(session["id"], session["summary_upto"]):
            if before_id is not None and m["id"] >= before_id:
                break
            if m["direction"] == "in" and m["channel"] in ("web", "telegram", "cli"):
                last_user = m["text"]  # 旧消息没有 sent_tail 时的回退
            if m["channel"] == "llm" and m["meta"].get("purpose") in ("plan", "note"):
                tail = m["meta"].get("sent_tail") or last_user
                pairs.append((m, {"role": "user", "content": tail}))
        return pairs

    def history(self, session: dict | None, before_id: int | None) -> list[dict]:
        if not session:
            return []
        out: list[dict] = []
        for m, user in self._pairs(session, before_id):
            out += [user, {"role": "assistant", "content": self._assistant_text(m)}]
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
        # 精简：待审阅轮给完整 prompt（可能要改它），生成中只给一行，已决定的只留最近 2 轮的决定
        rounds = self.store.rounds(project_id)
        closed = [r for r in rounds if r["state"] == "closed"][:2]
        for r in [r for r in rounds if r["state"] != "closed"] + closed:
            head = f"- 轮 {r['id']}（{ROUND_ZH[r['state']]}，{r['template']}）"
            prompt = r["base_params"].get("prompt", "")
            if r["state"] == "review":
                tasks = self.store.round_tasks(r["id"])
                cands = "，".join(f"#{t['idx']}=@task:{t['id']}" for t in tasks if t["outputs"])
                lines.append(f"{head}候选：{cands or '无'}\n  prompt: {prompt}")
            elif r["state"] == "generating":
                lines.append(head)
            else:
                d = r["decision"] or {}
                dec = d.get("action", "") + (f" #{d['idx']}" if d.get("idx") else "")
                note = f"（{d['note']}）" if d.get("note") else ""
                lines.append(f"{head}决定：{dec}{note}；prompt 摘要：{prompt[:160]}")
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
        live = self.live_state(project_id)
        fast = " /no_think" if self.cfg.planner_fast else ""
        final = f"## 当前状态（实时）\n{live}\n\n## 本次输入\n{purpose_text}{fast}"
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
        inflight = self.resources.llm.inference() if self.resources else contextlib.nullcontext()
        with inflight:  # 推理进行中，渲染把关会等它答完再卸载 LLM
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
                sent_tail=messages[-1]["content"],
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
        steps = t.slots.get("steps")
        steps_hint = (
            f"只有批注涉及细节程度时才给 steps（{steps.min:g}–{steps.max:g}），否则填 0。"
            if steps
            else "该模板没有 steps 参数，steps 填 0。"
        )
        task = (
            f"把批注翻译成参数修改。模板 {template}，可改参数：{', '.join(editable)}\n"
            f"当前参数：{json.dumps(shown, ensure_ascii=False)}\n批注：{note}\n"
            "输出 prompt 为修改后的完整新 prompt（不需要改则为空字符串）；" + steps_hint
        )
        msgs = self.build(task, project_id, session_id, before_id)
        data = self._call("note", msgs, DELTA_SCHEMA, project_id, session_id)
        delta = {}
        if data.get("prompt"):
            delta["prompt"] = data["prompt"]
        if data.get("steps") and steps:
            try:  # 越界或模板不支持的 steps 直接忽略，保留 prompt 的修改
                self._validate(template, {**shown, "steps": data["steps"]}, None, partial=True)
                delta["steps"] = data["steps"]
            except ParamError:
                pass
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
        pairs = self._pairs(session)
        keep = max(1, self.cfg.session_keep_messages // 2)  # 按“轮”保留：一问一答
        if len(pairs) <= keep:
            return False
        old, upto = pairs[:-keep], pairs[-keep - 1][0]["id"]
        lines = [
            f"用户（含当时状态）：{user['content']}\n规划器：{self._assistant_text(m)}"
            for m, user in old
        ]
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
            tr("log_summary", n=len(old)),
            session["project_id"],
            session_id=session_id,
        )
        self.prewarm(session_id)
        return True

    def prewarm(self, session_id: str | None) -> ChatResult | None:
        """重发上一次规划请求的完整内容，把它写回缓存；下一次请求正好是它的延长。"""
        if not session_id or not self.available():
            return None
        s = self.store.get_session(session_id)
        if s is None:
            return None
        pairs = self._pairs(s)
        msgs = [{"role": "system", "content": self.system_prompt(s["project_id"], s)}]
        for m, user in pairs[:-1]:
            msgs += [user, {"role": "assistant", "content": self._assistant_text(m)}]
        msgs.append(pairs[-1][1] if pairs else {"role": "user", "content": "（预热）"})
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
