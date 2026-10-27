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

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import ROOT
from .i18n import tr
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

        if tpl.kind == "video" and unmeasured and not explicit_tier(ctx["origin_text"], tier):
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
        if explicit_pick(ctx["origin_text"], a["idx"]):  # 你在原话里点名了 → 视为已批准
            return Done(_select(ctx, a))
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

    def ask_user(ctx, a):
        return Pending("user", f"ask:{ctx['run_id']}", a["question"])

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

    def _complete_prefix(self, msgs: list[dict]) -> list[dict]:
        """去掉末尾未完成的 tool_calls（结果没回来），保证历史在协议上合法。"""
        out = list(msgs)
        while out:
            last = out[-1]
            if last["role"] == "assistant" and last["tool_calls"]:
                out.pop()
                continue
            if last["role"] == "tool" and last["status"] != "done":
                while out and out[-1]["role"] == "tool":
                    out.pop()
                continue
            break
        return out

    def messages_for(self, run: dict) -> list[dict]:
        session = self.store.get_session(run["session_id"]) if run["session_id"] else None
        msgs = [{"role": "system", "content": self.system_prompt(run["project_id"], session)}]
        upto = (session and self.store.get_kv(f"agent_upto:{session['id']}")) or 0
        for r in self.store.runs(run["session_id"]):
            if r["id"] == run["id"]:
                break
            if r["created"] <= upto:
                continue
            msgs += [self._wire(m) for m in self._complete_prefix(self.store.agent_msgs(r["id"]))]
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
    def step(self, run_id: str) -> None:
        with self._lock:
            run = self.store.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN:
                return
            if self._unresolved(run_id):
                return  # 还有工具结果没回来，等事件 / 审批
            rm = self.studio.orch.resources
            ok, why = rm.llm_admit()
            if not ok:  # 结果先暂存，资源允许了再交回 LLM
                self.store.update_run(run_id, status="waiting_resources", waiting={"reason": why})
                return
            if run["steps"] >= self.max_steps:
                self._finish(run, tr("agent_step_budget", n=self.max_steps), "done")
                return
            self.store.update_run(run_id, status="running", waiting={})
        say = self.say_for(run["channel"])
        try:
            with rm.llm.inference():
                res = self.planner.backend.chat(self.messages_for(run), None, tools=self.spec)
        except Exception as e:  # LLM 出错：记录并告诉你，运行标为失败
            log.exception("agent LLM call failed")
            self._finish(run, tr("planner_error", e=e), "failed")
            return
        calls = res.tool_calls
        self.store.add_agent_msg(
            run_id, "assistant", res.text or None, tool_calls=calls or None, **res.stats()
        )
        self.store.update_run(run_id, steps=run["steps"] + 1)
        if not calls:
            self.studio.log(
                "llm",
                "out",
                res.text or "",
                run["project_id"],
                self.planner.backend.name,
                run_id=run_id,
                purpose="agent",
                **res.stats(),
            )
            self._finish(run, None, "done", say_text=res.text)
            return
        for c in calls:
            self._execute(run, c, say)
        self._advance(run_id)

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
        try:
            out = tool.handler(ctx, args)
        except (StudioError, ParamError, KeyError, ValueError) as e:
            out = Done({"error": str(e)})
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

    def on_round_ready(self, rnd: dict) -> None:
        self.resolve(f"round:{rnd['id']}", _round_view(self.store, rnd))

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
        for run in self.store.runs(session_id, "waiting_user"):
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
                    self.resolve(key, _round_view(self.store, rnd))
        for run in self.store.runs(None, "running", "waiting_resources"):
            if not self._unresolved(run["id"]):
                self.enqueue(lambda rid=run["id"]: self.step(rid))

    def cancel(self, run_id: str) -> None:
        self.store.update_run(run_id, status="cancelled")
        for m in self._unresolved(run_id):
            self.store.update_agent_msg(m["id"], content='{"cancelled": true}', status="done")
