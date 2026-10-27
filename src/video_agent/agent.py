"""Agent 运行时（A1 + A2）：LLM 发出 tool call，编排器执行，结果按资源情况择机交回 LLM。

一次运行（run）：
    你的话 → [LLM → tool_calls → 编排器执行 → 结果落库]* → 最终回复
每条消息（user / assistant+tool_calls / tool）逐字落库：既保证前缀缓存（只延长），也保证崩溃后能接着跑。

暂停与恢复（运行不占线程，状态都在数据库里）：
- waiting_resources：结果都齐了，但现在不适合唤醒 LLM（视频渲染独占内存）→ 渲染结束时恢复。
- waiting_event：在等某一轮渲染完成（wait_for_round）→ 该轮进入待审阅时带结果恢复。
- waiting_user：在等你批准（选版 / 锁定 / 未实测视频档位）或回答问题 → 你点按钮或回话后恢复。

工具分级：只读（立即）、动作（经 Studio，带幂等记录）、等待（事件唤醒）、需审批（按钮）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import jsonschema

from .config import ROOT
from .i18n import tr
from .policy import Policies, classify_round
from .production import ProductionError
from .render import ParamError
from .studio import StudioError

log = logging.getLogger("aco_lw.agent")
RULES_FILE = ROOT / "prompts" / "agent_rules.md"
TERMINAL_RUN = {"done", "failed", "cancelled"}
MAX_RESULT_CHARS = 4000


# ---------------- 工具定义 ----------------
@dataclass
class Done:
    result: dict


@dataclass
class Pending:
    kind: str  # event | approval | user
    key: str  # round:<id> | approval:<msg id 由运行时补> | ask:<run id>
    message: str | None = None  # 发给你的消息（审批 / 提问）
    buttons: list | None = None
    on_approve: dict | None = None  # 审批通过后要执行的动作 {"tool": ..., "args": ...}
    info: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    kind: str  # read | act | wait | approval | user
    handler: Callable


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}


def _round_view(store, rnd: dict) -> dict:
    tasks = store.round_tasks(rnd["id"])
    return {
        "round_id": rnd["id"],
        "state": rnd["state"],
        "template": rnd["template"],
        "tier": rnd["tier"],
        "title": rnd["title"],
        "decision": rnd["decision"] or None,
        "prompt": (rnd["base_params"].get("prompt") or "")[:600],
        "candidates": [
            {
                "idx": t["idx"],
                "task_id": t["id"],
                "state": t["state"],
                "outputs": [o["kind"] for o in t["outputs"]],
                "error": t["error"],
                "exec_s": (t["metrics"] or {}).get("exec_s"),
                "seed": t["params"].get("seed"),
            }
            for t in tasks
        ],
    }


def explicit_pick(text: str, idx: int) -> bool:
    """你在原话里明确点名了要选 / 锁哪一张（如“就用 #2”“2号锁成角色”）。"""
    t = text.lower()
    named = re.search(rf"(#\s*{idx}\b|{idx}\s*号|no\.?\s*{idx}\b|number\s*{idx}\b)", t)
    verbs = ("选", "锁", "用", "就要", "pick", "lock", "use", "choose", "select")
    return bool(named) and any(v in t for v in verbs)


def build_tools(rt: AgentRuntime) -> dict[str, Tool]:
    studio, store = rt.studio, rt.studio.store

    def get_status(ctx, a):
        rm = studio.orch.resources
        snap = rm.snap()
        plan = rm.plan(store.by_state("draft"))[:6]
        return Done(
            {
                "memory_free_gib": snap["avail_gib"],
                "pressure": snap["pressure"],
                "render_active": rm.render_active,
                "queued": len(store.by_state("draft")),
                "queue_eta": [
                    {
                        "task_id": p["task_id"],
                        "template": p["template"],
                        "tier": p["tier"],
                        "est_s": p["exec_s"],
                    }
                    for p in plan
                ],
                "llm_mode": rt.planner.mode,
            }
        )

    def list_templates(ctx, a):
        out = []
        for t in studio.orch.registry.builtin_and_user().values():
            out.append(
                {
                    "name": t.name,
                    "kind": t.kind,
                    "title": t.title,
                    "tiers": {
                        k: {**v.values(), "measured": v.verified} for k, v in t.tiers.items()
                    },
                    "params": [n for n in t.slots if n not in ("width", "height", "seconds")],
                }
            )
        return Done({"templates": out})

    def get_project(ctx, a):
        pid = ctx["project_id"]
        pack = store.pack(pid)
        return Done(
            {
                "project": store.get_project(pid)["name"],
                "baseline_pack": {
                    r: {"task_id": x["task_id"], "kind": x["kind"]}
                    for r, x in (pack["assets"] if pack else {}).items()
                },
                "memory": [m["text"] for m in store.memories(pid, "active")],
                "rounds": [_round_view(store, r) for r in store.rounds(pid)[:6]],
            }
        )

    def get_round(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None:
            return Done({"error": f"round {a['round_id']} not found"})
        return Done(_round_view(store, rnd))

    def queue_round(ctx, a):
        tpl = studio.orch.registry.template(a["template"])
        params = {"prompt": a["prompt"]} if a.get("prompt") else {}
        if a.get("image"):
            params["image"] = a["image"]
        if a.get("seed"):
            params["seed"] = a["seed"]
        tier = a.get("tier") or None
        if tier and tier not in tpl.tiers:
            return Done({"error": f"unknown tier {tier}; options: {list(tpl.tiers)}"})
        unmeasured = bool(tier) and not tpl.tiers[tier].verified
        from .daemon import explicit_tier

        rule = rt.policies.approval("queue_round", "unmeasured_video")
        named = explicit_tier(ctx["origin_text"], tier) if tier else False
        if (
            tpl.kind == "video"
            and unmeasured
            and (rule == "always" or (rule == "unless_named" and not named))
        ):
            return Pending(
                "approval",
                "",
                tr("agent_tier_approval", t=tier, tpl=tpl.name),
                on_approve={"tool": "queue_round_approved", "args": a},
            )
        return Done(_queue(ctx, a, params, tier, tpl.kind == "image" or unmeasured))

    def _queue(ctx, a, params, tier, allow_unverified):
        rnd = studio.start_round(
            a["template"],
            params,
            a.get("n") or None,
            tier,
            a.get("title"),
            origin=ctx["channel"],
            request=f"agent:{ctx['run_id']}:{ctx['call_id']}",
            parent_round=a.get("parent_round"),
            project_id=ctx["project_id"],
            allow_unverified=allow_unverified,
        )
        est = studio.orch.resources.estimator.estimate(
            studio.orch.registry.template(rnd["template"]), rnd["tier"]
        )
        return {
            "round_id": rnd["id"],
            "n": rnd["n"],
            "tier": rnd["tier"],
            "estimated_seconds_each": round(est.exec_s),
        }

    def queue_round_approved(ctx, a):
        params = {"prompt": a["prompt"]} if a.get("prompt") else {}
        if a.get("image"):
            params["image"] = a["image"]
        return Done(_queue(ctx, a, params, a.get("tier"), True))

    def wait_for_round(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None:
            return Done({"error": f"round {a['round_id']} not found"})
        if rnd["state"] != "generating":
            return Done(_round_view(store, rnd))
        return Pending("event", f"round:{rnd['id']}")

    def redo_round(ctx, a):
        new = studio.redo(a["round_id"], origin=ctx["channel"])
        return Done({"new_round_id": new["id"], "n": new["n"]})

    def refine_candidate(ctx, a):
        new = studio.refine(
            a["round_id"], a["idx"], {"prompt": a["prompt"]}, a.get("note"), origin=ctx["channel"]
        )
        return Done({"new_round_id": new["id"], "n": new["n"], "kept_seed_of": a["idx"]})

    def reject_round(ctx, a):
        rnd = studio.reject(a["round_id"], a.get("note"), origin=ctx["channel"])
        return Done({"round_id": rnd["id"], "state": rnd["state"]})

    def propose_selection(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None or rnd["state"] != "review":
            return Done({"error": "round is not waiting for review"})
        role = a.get("role") or None
        rule = rt.policies.approval("propose_selection")
        if rule == "never" or (
            rule == "unless_named" and explicit_pick(ctx["origin_text"], a["idx"])
        ):
            return Done(_select(ctx, a))  # 策略允许 / 你在原话里点名了 → 视为已批准
        txt = tr(
            "agent_select_approval",
            idx=a["idx"],
            t=rnd["title"] or rnd["template"],
            r=(tr("log_lock_suffix", r=role) if role else ""),
            why=a.get("reason") or "",
        )
        return Pending(
            "approval",
            "",
            txt,
            on_approve={"tool": "select_approved", "args": a},
            info={"round_id": rnd["id"]},
        )

    def _select(ctx, a):
        rnd = studio.select(
            a["round_id"], a["idx"], role=a.get("role") or None, origin=ctx["channel"]
        )
        return {"selected": a["idx"], "locked_as": a.get("role"), "round_id": rnd["id"]}

    def select_approved(ctx, a):
        return Done(_select(ctx, a))

    def propose_memory(ctx, a):
        mems = studio.propose_memories([a["text"]], {"run": ctx["run_id"]}, ctx["project_id"])
        for m in mems:
            ctx["say"](
                tr("mem_offer", t=m["text"]),
                [
                    [
                        (tr("mem_remember"), f"m:{m['id']}:accept"),
                        (tr("mem_ignore"), f"m:{m['id']}:ignore"),
                    ]
                ],
            )
        return Done({"proposed": [m["text"] for m in mems], "note": "applies after user confirms"})

    def queue_pipeline(ctx, a):
        steps = a.get("steps") or []
        if not steps:
            return Done({"error": "steps is empty"})
        for st in steps:  # 先整体校验，避免跑到一半才发现模板不存在
            studio.orch.registry.template(st["template"])
        rnd = _queue_step(ctx, steps[0], None)
        pipe = {"steps": steps, "index": 0, "rounds": [rnd["id"]]}
        return Pending("event", f"round:{rnd['id']}", info={"pipeline": pipe})

    def _queue_step(ctx, st, prev_round):
        params = {"prompt": st["prompt"]} if st.get("prompt") else {}
        image = st.get("image")
        if image == "@prev" and prev_round:  # 上一步的第一个成功产物
            ok = next(t for t in store.round_tasks(prev_round) if t["outputs"])
            image = f"@task:{ok['id']}"
        if image:
            params["image"] = image
        return studio.start_round(
            st["template"],
            params,
            st.get("n") or None,
            st.get("tier") or None,
            st.get("title"),
            origin=ctx["channel"],
            request=f"pipeline:{ctx['run_id']}",
            parent_round=prev_round,
            project_id=ctx["project_id"],
            allow_unverified=True,
        )

    rt._queue_step = _queue_step

    def ask_user(ctx, a):
        return Pending("user", f"ask:{ctx['run_id']}", a["question"])

    # ---- 制片流水线（阶段 3）：agent 只读计划、按计划开跑或转达你的审核，不自建状态机 ----
    def _prod():
        prod = getattr(rt, "production", None)
        if prod is None:
            raise ValueError("production engine not available (runs inside va daemon)")
        return prod

    def _plan_view(plan: dict) -> dict:
        return {
            "production_id": plan["production"]["id"],
            "title": plan["production"]["title"],
            "workflow": plan["workflow"]["name"],
            "next_action": plan["next_action"],
            "stages": [
                {"id": s["id"], "state": s["state"], "counts": s["counts"], "confirm": s["confirm"]}
                for s in plan["stages"]
            ],
        }

    def list_workflows(ctx, a):
        return Done(
            {
                "workflows": [
                    {
                        "name": w.name,
                        "title": w.title,
                        "description": w.description,
                        "settings": w.settings,
                        "stages": [s.id for s in w.stages],
                    }
                    for w in _prod().workflows.values()
                ],
                "productions": [
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "workflow": p["workflow"],
                        "state": p["state"],
                    }
                    for p in store.productions(ctx["project_id"])
                ][:10],
            }
        )

    def create_production(ctx, a):
        prod = _prod().create(
            a["workflow"],
            a["title"],
            {"source": a["source"]},
            a.get("settings") or {},
            ctx["project_id"],
        )
        return Done(_plan_view(_prod().plan(prod["id"])))

    def get_production_plan(ctx, a):
        return Done(_plan_view(_prod().plan(a["production_id"])))

    def run_stage(ctx, a):
        p = _prod()
        plan = p.plan(a["production_id"])
        st = next((s for s in plan["stages"] if s["id"] == a["stage"]), None)
        if st and st["confirm"] and not a.get("user_confirmed"):
            return Done(
                {
                    "error": f"stage {a['stage']} needs the user's go-ahead (long renders); "
                    "ask them, then call again with user_confirmed=true"
                }
            )
        made = p.run_stage(a["production_id"], a["stage"], a.get("keys"), a.get("note"), "agent")
        return Done({"started": len(made), "plan": _plan_view(p.plan(a["production_id"]))})

    def review_stage(ctx, a):
        p = _prod()
        art = p.review(
            a["production_id"],
            a["stage"],
            a["action"],
            a.get("note"),
            None,
            a.get("key") or "main",
            ctx["channel"],
        )
        return Done(
            {
                "artifact": {k: art[k] for k in ("stage", "key", "version", "status")},
                "plan": _plan_view(p.plan(a["production_id"])),
            }
        )

    def update_production_settings(ctx, a):
        prod = _prod().update_settings(a["production_id"], a["settings"])
        return Done({"settings": prod["settings"]})

    round_id = {"round_id": S}
    tools = [
        Tool(
            "get_status",
            "Memory, queue with ETA, active render, LLM mode.",
            _obj({}),
            "read",
            get_status,
        ),
        Tool(
            "list_templates",
            "Available templates, their tiers (resolution × duration; "
            "measured or not) and parameters.",
            _obj({}),
            "read",
            list_templates,
        ),
        Tool(
            "get_project",
            "Current project: baseline pack, confirmed memory, recent rounds with candidates.",
            _obj({}),
            "read",
            get_project,
        ),
        Tool(
            "get_round",
            "One round with its candidates.",
            _obj(round_id, ["round_id"]),
            "read",
            get_round,
        ),
        Tool(
            "queue_round",
            "Queue a round of candidates (renders in the background).",
            _obj(
                {
                    "template": S,
                    "prompt": S,
                    "image": S,
                    "tier": S,
                    "n": I,
                    "seed": I,
                    "title": S,
                    "parent_round": S,
                },
                ["template", "prompt"],
            ),
            "act",
            queue_round,
        ),
        Tool(
            "wait_for_round",
            "Wait until a round has finished rendering and get its candidates. "
            "You are paused meanwhile.",
            _obj(round_id, ["round_id"]),
            "wait",
            wait_for_round,
        ),
        Tool(
            "redo_round",
            "Discard a round under review and render it again with new seeds.",
            _obj(round_id, ["round_id"]),
            "act",
            redo_round,
        ),
        Tool(
            "refine_candidate",
            "Keep candidate idx's seed, change the prompt, new round.",
            _obj({"round_id": S, "idx": I, "prompt": S, "note": S}, ["round_id", "idx", "prompt"]),
            "act",
            refine_candidate,
        ),
        Tool(
            "reject_round",
            "Reject a round under review with a note.",
            _obj({"round_id": S, "note": S}, ["round_id"]),
            "act",
            reject_round,
        ),
        Tool(
            "propose_selection",
            "Propose selecting candidate idx (optionally locking it into the "
            "baseline pack as role). The user approves.",
            _obj({"round_id": S, "idx": I, "role": S, "reason": S}, ["round_id", "idx"]),
            "approval",
            propose_selection,
        ),
        Tool(
            "propose_memory",
            "Propose a durable project fact the user stated.",
            _obj({"text": S}, ["text"]),
            "act",
            propose_memory,
        ),
        Tool(
            "queue_pipeline",
            "Run several rounds in order without consulting you between them (per policy): each "
            'step {template, prompt, image, tier, n, title}; image "@prev" uses the previous '
            "step's first output. Returns when all steps finish or one fails.",
            _obj(
                {
                    "steps": {
                        "type": "array",
                        "items": _obj(
                            {"template": S, "prompt": S, "image": S, "tier": S, "n": I, "title": S},
                            ["template", "prompt"],
                        ),
                    }
                },
                ["steps"],
            ),
            "wait",
            queue_pipeline,
        ),
        Tool(
            "list_workflows",
            "Production workflows (e.g. novel-to-film) and this project's productions.",
            _obj({}),
            "read",
            list_workflows,
        ),
        Tool(
            "create_production",
            "Start a multi-stage production (e.g. novel → film) from source text. Planning "
            "stages start automatically; every stage waits for the user's review before the next.",
            _obj(
                {
                    "workflow": S,
                    "title": S,
                    "source": S,
                    "settings": {
                        "type": "object",
                        "description": "e.g. video_tier, image_candidates, subtitles",
                    },
                },
                ["workflow", "title", "source"],
            ),
            "act",
            create_production,
        ),
        Tool(
            "get_production_plan",
            "The authoritative plan of a production: each stage's state and the next action. "
            "Always read it instead of guessing progress.",
            _obj({"production_id": S}, ["production_id"]),
            "read",
            get_production_plan,
        ),
        Tool(
            "run_stage",
            "Run (or re-run) a production stage — by default only missing / stale / failed items. "
            "Stages marked confirm (videos) need user_confirmed=true, set only when the user said so.",
            _obj(
                {
                    "production_id": S,
                    "stage": S,
                    "keys": {"type": "array", "items": S},
                    "note": S,
                    "user_confirmed": {"type": "boolean"},
                },
                ["production_id", "stage"],
            ),
            "act",
            run_stage,
        ),
        Tool(
            "review_stage",
            "Relay the user's review of a planning stage: approve, redo (with note), reject, or "
            "keep (still valid although upstream changed). A change that concerns many shots (e.g. "
            "'all shots in Mandarin') is a redo of the storyboard stage with that note; only shots "
            "whose text changes get re-rendered. Render stages are reviewed on their rounds.",
            _obj(
                {
                    "production_id": S,
                    "stage": S,
                    "action": {"type": "string", "enum": ["approve", "redo", "reject", "keep"]},
                    "note": S,
                    "key": S,
                },
                ["production_id", "stage", "action"],
            ),
            "act",
            review_stage,
        ),
        Tool(
            "update_production_settings",
            "Change a production's settings (e.g. language for dialogue, video_tier). Applies to "
            "items run from now on; approved ones are not re-rendered automatically.",
            _obj({"production_id": S, "settings": {"type": "object"}}, ["production_id", "settings"]),
            "act",
            update_production_settings,
        ),
        Tool(
            "ask_user",
            "Ask the user one short question and wait for the answer.",
            _obj({"question": S}, ["question"]),
            "user",
            ask_user,
        ),
    ]
    hidden = [
        Tool("queue_round_approved", "", _obj({}), "act", queue_round_approved),
        Tool("select_approved", "", _obj({}), "act", select_approved),
    ]
    return {t.name: t for t in tools + hidden}


def tools_spec(tools: dict[str, Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools.values()
        if t.description
    ]


# ---------------- 运行时 ----------------
class AgentRuntime:
    def __init__(
        self,
        studio,
        planner,
        enqueue: Callable[[Callable[[], None]], None],
        say_for: Callable[[str], Callable],
        max_steps: int = 16,
    ):
        """enqueue：把一步放进规划器队列（单线程）；say_for(channel)：给某个渠道发消息的函数。"""
        self.studio, self.planner, self.store = studio, planner, studio.store
        self.enqueue, self.say_for, self.max_steps = enqueue, say_for, max_steps
        self.cfg = studio.orch.cfg
        self.policies = Policies.load(studio.orch.cfg.data_dir)
        self.tools = build_tools(self)
        self.spec = tools_spec(self.tools)
        self._lock = threading.RLock()
        studio.orch.resources.listeners.append(self.on_resources_freed)

    # ---- 上下文（稳定前缀 + 逐字复现） ----
    def system_prompt(self, project_id: str, session: dict | None) -> str:
        # agent 规则 + 与规划器相同的模板说明、项目记忆、会话摘要（稳定前缀）
        return self.planner.system_prompt(project_id, session, RULES_FILE.read_text().strip())

    @staticmethod
    def _wire(m: dict) -> dict:
        if m["role"] == "assistant":
            out = {"role": "assistant", "content": m["content"]}
            if m["meta"].get("reasoning"):  # 本次运行内逐字回放思考内容（与生成时一致）
                out["reasoning_content"] = m["meta"]["reasoning"]
            if m["meta"].get("anthropic"):  # Claude 生成的回合：原始内容块原样回放
                out["_anthropic"] = m["meta"]["anthropic"]
            if m["tool_calls"]:
                out["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in m["tool_calls"]
                ]
            return out
        if m["role"] == "tool":
            return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
        return {"role": m["role"], "content": m["content"]}

    def _compact(self, run: dict) -> list[dict]:
        """已结束的运行压成稳定的两条（原话 + 最终回复）。

        实测：模型的聊天模板在新的用户消息到来后会去掉更早轮次的思考内容，跨运行的前缀
        本来就无法逐字延长；压成稳定形式后，每次只重算上一轮的这两条，更早的都命中缓存。
        """
        final = next(
            (
                m["content"]
                for m in reversed(self.store.agent_msgs(run["id"]))
                if m["role"] == "assistant" and not m["tool_calls"] and m["content"]
            ),
            f"(run {run['status']})",
        )
        return [
            {"role": "user", "content": f"## User\n{run['origin_text']}"},
            {"role": "assistant", "content": final},
        ]

    def messages_for(self, run: dict) -> list[dict]:
        session = self.store.get_session(run["session_id"]) if run["session_id"] else None
        msgs = [{"role": "system", "content": self.system_prompt(run["project_id"], session)}]
        upto = (session and self.store.get_kv(f"agent_upto:{session['id']}")) or 0
        for r in self.store.runs(run["session_id"]):
            if r["id"] == run["id"]:
                break
            if r["created"] <= upto:
                continue
            msgs += self._compact(r)
        msgs += [self._wire(m) for m in self.store.agent_msgs(run["id"])]
        return msgs

    # ---- 启动 ----
    def start(
        self,
        text: str,
        channel: str,
        project_id: str,
        session_id: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        run = self.store.create_run(project_id, session_id, channel, text)
        att = "".join(
            f"\n[attached image @upload:{a['id']} {a.get('name', '')}]" for a in attachments or []
        )
        live = self.planner.live_state(project_id)
        content = f"## Live state\n{live}\n\n## User\n{text}{att}"
        self.store.add_agent_msg(run["id"], "user", content)
        self.enqueue(lambda: self.step(run["id"]))
        return run

    # ---- 一步 ----
    # ---- 选后端：本地优先；资源不够 / 本地不可用时按配置回退到在线模型或 agent CLI ----
    def _api_backend(self, name: str):
        cache = self.__dict__.setdefault("_api_backends", {})
        if name not in cache:
            from .planner import make_named_backend

            cache[name] = make_named_backend(self.cfg, self.store, name)
        return cache[name]

    def _fallback(self, run: dict, why: str):
        """返回 ("backend", 后端) | ("delegate", CLI 名) | ("park", 原因)。"""
        chain = list(self.cfg.llm_fallback or [])
        if not chain:
            return "park", why
        if not self.cfg.allow_online:  # 隐私开关没开：不往外发
            return "park", f"{why}; online fallback is configured but allow_online is off"
        fresh = len(self.store.agent_msgs(run["id"])) == 1  # 还没开始：可以整个交给 CLI
        for name in chain:
            if name in ("anthropic", "openrouter", "openai_compatible"):
                return "backend", self._api_backend(name)
            if name in ("claude_code", "codex") and fresh:
                return "delegate", name
        return "park", why

    def _choose(self, run: dict):
        primary = self.planner.backend
        if not getattr(primary, "local", False):
            return "backend", primary  # 主后端本身就在线：不占本地内存，不需要把关
        ok, why = self.studio.orch.resources.llm_admit()
        if ok:
            return "backend", primary
        return self._fallback(run, why)

    def step(self, run_id: str) -> None:
        with self._lock:
            run = self.store.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN or run["status"] == "delegated":
                return
            if self._unresolved(run_id):
                return  # 还有工具结果没回来，等事件 / 审批
            if run["steps"] >= self.max_steps:
                self._finish(run, tr("agent_step_budget", n=self.max_steps), "done")
                return
            kind, what = self._choose(run)
            if kind == "park":  # 结果先暂存，资源允许了再交回 LLM
                self.store.update_run(run_id, status="waiting_resources", waiting={"reason": what})
                return
            if kind == "delegate":
                self._delegate(run, what)
                return
            self.store.update_run(run_id, status="running", waiting={})
        backend = what
        say = self.say_for(run["channel"])
        rm = self.studio.orch.resources
        try:
            res = self._call(backend, run, rm)
        except Exception as e:  # 本地 LLM 出错 / 不可用：按配置回退一次，否则标为失败
            kind, alt = self._fallback(run, f"{backend.name} failed: {e}")
            if kind == "backend" and alt is not backend:
                log.warning(
                    "agent backend %s failed, falling back to %s: %s", backend.name, alt.name, e
                )
                try:
                    backend, res = alt, self._call(alt, run, rm)
                except Exception as e2:
                    log.exception("agent fallback call failed")
                    self._finish(run, tr("planner_error", e=e2), "failed")
                    return
            elif kind == "delegate":
                self._delegate(run, alt)
                return
            else:
                log.exception("agent LLM call failed")
                self._finish(run, tr("planner_error", e=e), "failed")
                return
        if backend is not self.planner.backend:
            self._log_policy(
                run, f"local LLM unavailable → this step used {res.backend or backend.name}"
            )
        calls = res.tool_calls
        extra = (
            {"anthropic": res.raw_content} if (res.backend or "").startswith("anthropic") else {}
        )
        self.store.add_agent_msg(
            run_id,
            "assistant",
            res.text or None,
            tool_calls=calls or None,
            reasoning=res.reasoning,
            **extra,
            **res.stats(),
        )
        self.store.update_run(run_id, steps=run["steps"] + 1)
        if not calls:
            self.studio.log(
                "llm",
                "out",
                res.text or "",
                run["project_id"],
                backend.name,
                run_id=run_id,
                purpose="agent",
                **res.stats(),
            )
            self._finish(run, None, "done", say_text=res.text)
            return
        for c in calls:
            self._execute(run, c, say)
        self._advance(run_id)

    def _call(self, backend, run: dict, rm):
        inflight = (
            rm.llm.inference() if getattr(backend, "local", False) else contextlib.nullcontext()
        )
        with inflight:
            return backend.chat(self.messages_for(run), None, tools=self.spec)

    # ---- 交给本机 agent CLI（Claude Code / Codex） ----
    def _delegate(self, run: dict, name: str) -> None:
        self.store.update_run(run["id"], status="delegated", waiting={"agent": name})
        self._log_policy(run, f"local LLM unavailable → whole request delegated to {name}")
        threading.Thread(
            target=self._run_cli, args=(run["id"], name), daemon=True, name=f"agent-cli-{run['id']}"
        ).start()

    def cli_prompt(self, run: dict) -> str:
        session = self.store.get_session(run["session_id"]) if run["session_id"] else None
        parts = [self.system_prompt(run["project_id"], session)]
        for r in self.store.runs(run["session_id"]):
            if r["id"] == run["id"]:
                break
            if r["status"] in TERMINAL_RUN:
                c = self._compact(r)
                parts.append(f"{c[0]['content']}\n## Your earlier answer\n{c[1]['content']}")
        parts.append(self.store.agent_msgs(run["id"])[0]["content"])
        parts.append(
            "## Environment\nYou run inside aco_lw. Use only the aco_lw tools; you have "
            "no shell or file access. When finished, reply with the final message for the "
            "user (it is shown to them as-is)."
        )
        return "\n\n".join(parts)

    def _run_cli(self, run_id: str, name: str) -> None:

        run = self.store.get_run(run_id)
        url = f"http://127.0.0.1:{self.cfg.web_port}"
        try:
            text, stats = self.cli_runner(name).run(run_id, self.cli_prompt(run), url)
            status = "done"
        except Exception as e:  # CLI 失败：告诉你原因，运行标为失败
            log.exception("agent CLI %s failed", name)
            text, stats, status = tr("planner_error", e=e), {"via": name}, "failed"
        self.store.add_agent_msg(run_id, "assistant", text, **stats)
        self.studio.log(
            "llm", "out", text, run["project_id"], name, run_id=run_id, purpose="agent", **stats
        )
        self.store.update_run(run_id, status="running")  # 离开 delegated 后再结束
        self._finish(self.store.get_run(run_id), None, status, say_text=text)

    def cli_runner(self, name: str):
        from .agent_cli import AgentCLI

        return AgentCLI(name, self.cfg)

    def external_tool(self, run_id: str, name: str, args: dict, timeout: float = 1800) -> dict:
        """CLI agent 经 MCP 发来的工具调用：同样的执行策略、审批与等待，只是同步返回结果。"""
        run = self.store.get_run(run_id)
        if run is None or run["status"] != "delegated":
            return {"error": "this run is not delegated to an external agent"}
        call = {
            "id": f"ext_{uuid.uuid4().hex[:10]}",
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
        self._execute(run, call, self.say_for(run["channel"]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = next(
                (
                    x
                    for x in self.store.agent_msgs(run_id)
                    if x["role"] == "tool" and x["tool_call_id"] == call["id"]
                ),
                None,
            )
            if m and m["status"] == "done":
                try:
                    return json.loads(m["content"])
                except (TypeError, json.JSONDecodeError):
                    return {"result": m["content"]}
            time.sleep(1)
        return {
            "pending": True,
            "note": "still waiting (render or user decision); "
            "call the same tool again to keep waiting",
        }

    def _execute(self, run: dict, call: dict, say) -> None:
        tool = self.tools.get(call["name"])
        ctx = {
            "run_id": run["id"],
            "call_id": call["id"],
            "channel": run["channel"],
            "project_id": run["project_id"],
            "origin_text": run["origin_text"] or "",
            "say": say,
        }
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as e:
            return self._tool_done(run, call, {}, {"error": f"arguments are not valid JSON: {e}"})
        if tool is None or not tool.description:
            return self._tool_done(run, call, args, {"error": f"unknown tool {call['name']}"})
        denied = self.policies.check(call["name"], args)
        if denied:  # 执行策略：只拒绝，不替你改参数
            return self._tool_done(run, call, args, {"error": denied})
        try:
            out = tool.handler(ctx, args)
        except (StudioError, ParamError, KeyError, ValueError, ProductionError) as e:
            out = Done({"error": str(e)})
        except jsonschema.ValidationError as e:
            out = Done({"error": f"schema: {e.message}"})
        if isinstance(out, Done):
            return self._tool_done(run, call, args, out.result)
        mid = self.store.add_agent_msg(
            run["id"],
            "tool",
            None,
            tool_call_id=call["id"],
            name=call["name"],
            status="pending",
            kind=out.kind,
            key=out.key,
            on_approve=out.on_approve,
            info=out.info,
        )
        if out.kind == "approval":
            key = f"approval:{mid}"
            self.store.update_agent_msg(mid, meta={"key": key})
            say(
                out.message,
                [[(tr("btn_approve"), f"a:{mid}:yes"), (tr("btn_decline"), f"a:{mid}:no")]],
            )
        elif out.kind == "user":
            say(out.message)
        self._log_step(run, call, args, "…")

    def _tool_done(self, run: dict, call: dict, args: dict, result: dict) -> None:
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:  # 太长的结果截断后交回，原文留在 meta
            text = text[:MAX_RESULT_CHARS] + "…(truncated)"
        self.store.add_agent_msg(
            run["id"], "tool", text, tool_call_id=call["id"], name=call["name"], full=result
        )
        self._log_step(run, call, args, text)

    def _log_step(self, run: dict, call: dict, args, result: str) -> None:
        brief = ", ".join(
            f"{k}={str(v)[:40]}" for k, v in (args or {}).items() if k not in ("prompt",)
        )
        self.studio.log(
            "agent",
            "out",
            f"🔧 {call['name']}({brief}) → {result[:160]}",
            run["project_id"],
            run_id=run["id"],
            tool=call["name"],
        )

    def _unresolved(self, run_id: str) -> list[dict]:
        return [
            m
            for m in self.store.agent_msgs(run_id)
            if m["role"] == "tool" and m["status"] != "done"
        ]

    def _advance(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run and run["status"] == "delegated":
            return  # CLI agent 自己在等这个结果（external_tool 轮询），不要排本地步骤
        pending = self._unresolved(run_id)
        if not pending:
            self.store.update_run(run_id, status="running", waiting={})
            self.enqueue(lambda: self.step(run_id))
            return
        kinds = {m["meta"].get("kind") for m in pending}
        status = "waiting_user" if kinds & {"approval", "user"} else "waiting_event"
        self.store.update_run(
            run_id, status=status, waiting={"keys": [m["meta"].get("key") for m in pending]}
        )

    def _finish(self, run: dict, note: str | None, status: str, say_text: str | None = None):
        self.store.update_run(run["id"], status=status)
        text = say_text if say_text is not None else note
        if text:
            self.say_for(run["channel"])(text)
        if run["session_id"]:
            self.enqueue(lambda: self.prewarm(run["session_id"]))

    def prewarm(self, session_id: str) -> None:
        """为下一条消息预热缓存。

        实测（docs/baseline.md）：引擎在每个请求“最后一条消息之前”留检查点，新请求只有与某个旧请求
        一直相同到那里才复用。这里发一次 [稳定前缀 + 压缩历史 + 占位用户消息]，
        你的下一句话就会命中“占位消息之前”的检查点。资源不允许时跳过（不抢渲染）。
        """
        rm = self.studio.orch.resources
        if not rm.llm_admit()[0] or not self.planner.available():
            return
        session = self.store.get_session(session_id)
        if session is None:
            return
        msgs = [{"role": "system", "content": self.system_prompt(session["project_id"], session)}]
        upto = self.store.get_kv(f"agent_upto:{session_id}") or 0
        for r in self.store.runs(session_id):
            if r["created"] > upto and r["status"] in TERMINAL_RUN:
                msgs += self._compact(r)
        msgs.append({"role": "user", "content": "## Live state\n(warm-up)"})
        try:
            with rm.llm.inference():
                self.planner.backend.prewarm(msgs, tools=self.spec)
        except Exception:  # 预热失败不影响任何结果
            log.warning("agent prewarm failed", exc_info=True)

    # ---- 恢复与唤醒 ----
    def resolve(self, key: str, result: dict) -> int:
        """把等待中的工具结果补上（事件 / 审批 / 回答），能继续的运行排队继续。"""
        n = 0
        for m in self.store.pending_tool_msgs():
            if m["meta"].get("key") != key:
                continue
            self.store.update_agent_msg(
                m["id"],
                content=json.dumps(result, ensure_ascii=False),
                status="done",
                meta={"full": result},
            )
            n += 1
            self._advance(m["run_id"])
        return n

    def resolve_msg(self, m: dict, result: dict) -> None:
        self.store.update_agent_msg(
            m["id"],
            content=json.dumps(result, ensure_ascii=False),
            status="done",
            meta={"full": result},
        )
        self._advance(m["run_id"])

    def on_round_ready(self, rnd: dict) -> None:
        """渲染结果到了：按续跑策略决定交回 LLM、自动下一步、还是有限重试。"""
        key = f"round:{rnd['id']}"
        for m in self.store.pending_tool_msgs():
            if m["meta"].get("key") != key:
                continue
            run = self.store.get_run(m["run_id"])
            pipe = (m["meta"].get("info") or {}).get("pipeline")
            outcome = classify_round(self.store, rnd["id"])
            retries = m["meta"].get("retries", 0)
            last = bool(pipe) and pipe["index"] >= len(pipe["steps"]) - 1
            action = self.policies.on_round(outcome, retries, bool(pipe), last)
            if action == "retry":
                new = self._retry(rnd, run)
                if pipe:
                    pipe["rounds"][-1] = new["id"]
                self.store.update_agent_msg(
                    m["id"],
                    meta={
                        "key": f"round:{new['id']}",
                        "retries": retries + 1,
                        "info": {"pipeline": pipe} if pipe else {},
                    },
                )
                self._log_policy(run, f"{outcome} → retry {retries + 1} (same parameters)")
                continue
            if action == "auto_next":
                pipe["index"] += 1
                nxt = self._queue_step(
                    {
                        "channel": run["channel"],
                        "project_id": run["project_id"],
                        "run_id": run["id"],
                    },
                    pipe["steps"][pipe["index"]],
                    rnd["id"],
                )
                pipe["rounds"].append(nxt["id"])
                self.store.update_agent_msg(
                    m["id"],
                    meta={"key": f"round:{nxt['id']}", "retries": 0, "info": {"pipeline": pipe}},
                )
                self._log_policy(
                    run,
                    f"step {pipe['index']} of {len(pipe['steps'])} done "
                    "→ next step queued (no LLM)",
                )
                continue
            if pipe:
                result = {
                    "pipeline": [
                        _round_view(self.store, self.store.get_round(r)) for r in pipe["rounds"]
                    ],
                    "finished": action == "finished",
                    "outcome": outcome,
                }
            else:
                result = {**_round_view(self.store, rnd), "outcome": outcome}
            self.resolve_msg(m, result)

    def _retry(self, rnd: dict, run: dict) -> dict:
        tasks = self.store.round_tasks(rnd["id"])
        seeds = [t["params"].get("seed") for t in tasks]
        return self.studio.start_round(
            rnd["template"],
            rnd["base_params"],
            rnd["n"],
            rnd["tier"],
            rnd["title"],
            origin=run["channel"],
            request="policy retry",
            parent_round=rnd["id"],
            project_id=rnd["project_id"],
            allow_unverified=True,
            seeds=seeds if all(isinstance(x, int) for x in seeds) else None,
        )

    def _log_policy(self, run: dict, text: str) -> None:
        self.studio.log("agent", "out", f"⚙️ policy: {text}", run["project_id"], run_id=run["id"])

    def on_round_decided(self, rnd: dict) -> None:
        """你用普通审阅按钮做了决定：等这轮的批准请求也就有了答案。"""
        d = rnd["decision"] or {}
        for m in self.store.pending_tool_msgs():
            if (
                m["meta"].get("kind") == "approval"
                and m["meta"].get("info", {}).get("round_id") == rnd["id"]
            ):
                self.resolve(
                    m["meta"]["key"],
                    {
                        "user_decision": d.get("action"),
                        "selected": d.get("idx"),
                        "role": d.get("role"),
                    },
                )
        self.on_round_ready(rnd)

    def on_approval(self, msg_id: int, approve: bool) -> str:
        m = self.store.get_agent_msg(msg_id)
        if m is None or m["status"] == "done":
            return tr("agent_already_decided")
        run = self.store.get_run(m["run_id"])
        if not approve:
            self.resolve(m["meta"]["key"], {"approved": False, "note": "user declined"})
            return tr("agent_declined")
        act = m["meta"].get("on_approve") or {}
        ctx = {
            "run_id": run["id"],
            "call_id": m["tool_call_id"],
            "channel": run["channel"],
            "project_id": run["project_id"],
            "origin_text": run["origin_text"] or "",
            "say": self.say_for(run["channel"]),
        }
        try:
            out = self.tools[act["tool"]].handler(ctx, act["args"])
            result = {"approved": True, **out.result}
        except (StudioError, ParamError, KeyError, ValueError) as e:
            result = {"approved": True, "error": str(e)}
        self.resolve(m["meta"]["key"], result)
        return tr("agent_approved")

    def answer_pending_question(self, channel: str, session_id: str, text: str) -> bool:
        for run in self.store.runs(session_id, "waiting_user", "delegated"):
            if run["channel"] != channel:
                continue
            if self.resolve(f"ask:{run['id']}", {"answer": text}):
                return True
        return False

    def on_resources_freed(self) -> None:
        for run in self.store.runs(None, "waiting_resources"):
            self.enqueue(lambda rid=run["id"]: self.step(rid))

    def recover(self) -> None:
        """daemon 重启：能继续的运行继续；在等的轮若已完成，直接补上结果。"""
        for m in self.store.pending_tool_msgs():
            key = m["meta"].get("key") or ""
            if key.startswith("round:"):
                rnd = self.store.get_round(key.split(":", 1)[1])
                if rnd and rnd["state"] != "generating":
                    self.on_round_ready(rnd)
        for run in self.store.runs(None, "running", "waiting_resources"):
            if not self._unresolved(run["id"]):
                self.enqueue(lambda rid=run["id"]: self.step(rid))

    def cancel(self, run_id: str) -> None:
        self.store.update_run(run_id, status="cancelled")
        for m in self._unresolved(run_id):
            self.store.update_agent_msg(m["id"], content='{"cancelled": true}', status="done")
