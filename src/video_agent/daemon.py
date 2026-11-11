"""常驻服务 `va daemon`：Worker（串行出图）+ 规划器队列（按需唤醒 LLM）+ Telegram 监听 + Web。

- Worker 只经编排器执行 draft 任务；一轮候选全部跑完后通知审阅。
- Actions 是与渠道无关的入口：Telegram 与 Web 控制台共用，所有消息写入对话历史。
- 命令与按钮立即执行；自由文本进规划器队列。视频生成占内存时规划器排队，不抢占。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable

from . import config, i18n, memory
from .agent import AgentRuntime
from .comfy import ComfyBusy
from .i18n import tr
from .planner import MODES, Planner, PlannerUnavailable
from .production import Park, Production, load_workflows
from .render import ParamError
from .scheduler import Orchestrator, SchedulerBusy
from .skills import SkillLibrary
from .studio import Studio, StudioError
from .telegram import TelegramAPI, TelegramBot, lock_buttons

log = logging.getLogger("aco_lw")
Reply = Callable[..., None]


class Worker(threading.Thread):
    def __init__(
        self,
        orch: Orchestrator,
        studio: Studio,
        notify: Callable[[dict], None],
        stop: threading.Event,
    ):
        super().__init__(name="worker", daemon=True)
        self.orch, self.studio, self.notify, self.stop = orch, studio, notify, stop
        self.store = orch.store
        self.busy_kind: str | None = None
        self.current: dict | None = None
        self.hold: Callable[[dict], bool] = lambda task: (
            False
        )  # True：先别执行（例如所属制片已暂停）

    def run(self) -> None:
        try:
            for tid, outcome in self.orch.recover():
                log.info("Recovered %s: %s", tid, outcome)
        except SchedulerBusy as e:
            log.warning("Startup reconcile skipped: %s", e)
        self._refresh_rounds()
        last_reconcile = 0.0
        while not self.stop.is_set():
            if self.store.get_kv("paused"):
                self.stop.wait(2)
                continue
            if time.monotonic() - last_reconcile > 120:  # 定期收回“观察超时”后才跑完的任务
                last_reconcile = time.monotonic()
                for tid, state in self.orch.reconcile_timeouts():
                    log.info("Collected %s after timeout: %s", tid, state)
                    t = self.store.get(tid)
                    if t["round_id"]:
                        rnd, ready = self.studio.refresh_round(t["round_id"])
                        if ready:
                            self.notify(rnd)
            drafts = [t for t in self.store.by_state("draft") if not self.hold(t)]
            task = self.orch.resources.choose_next(drafts)
            if task is None:
                self.stop.wait(2)
                continue
            self.current, self.busy_kind = task, task["kind"]
            try:
                self.orch.execute(task["id"])
            except SchedulerBusy:
                log.info("Another orchestrator process is running (e.g. CLI); retrying later")
                self.stop.wait(5)
                continue
            except Exception:
                log.exception("Executing %s failed", task["id"])
                t = self.store.get(task["id"])
                if t["state"] in ("draft", "validated", "waiting_memory"):
                    self.store.transition(task["id"], "failed", "执行异常", error="worker error")
            finally:
                self.current, self.busy_kind = None, None
            self._after(task)

    def stop_current(self, only: Callable[[dict], bool] = lambda task: True) -> dict | None:
        """中断正在执行的任务（只中断自己提交的那个 job）；返回被中断的任务。"""
        cur = self.current
        if not cur or not only(cur):
            return None
        t = self.store.get(cur["id"])
        if t["prompt_id"]:
            try:
                tpl = self.orch.registry.template(t["template"])
                self.orch.executor_for(tpl).cancel(t["prompt_id"])
            except (ComfyBusy, OSError) as e:
                log.warning("Interrupting %s failed: %s", t["id"], e)
        return t

    def heartbeat(self) -> None:
        """独立线程写心跳：渲染时 Worker 卡在一次长任务里，心跳不能跟着停。"""
        while not self.stop.is_set():
            self.store.set_kv(
                "daemon",
                {
                    "pid": os.getpid(),
                    "ts": time.time(),
                    "busy": self.current and self.current["id"],
                    "paused": bool(self.store.get_kv("paused")),
                },
            )
            self.orch.resources.record()
            self.stop.wait(5)

    def _after(self, task: dict) -> None:
        if task["round_id"]:
            rnd, ready = self.studio.refresh_round(task["round_id"])
            if ready:
                self.notify(rnd)
        self._refresh_rounds()

    def _refresh_rounds(self) -> None:
        for r in self.store.rounds(state="generating"):
            rnd, ready = self.studio.refresh_round(r["id"])
            if ready:
                self.notify(rnd)


EXPLICIT_HD = ("高清", "1080", "720p", "4k", "full hd")


def explicit_tier(text: str, tier: str) -> bool:
    """你在这句话里明确点名了档位（如 hd、高清、10s），视为批准使用未实测档位。"""
    t = text.lower()
    parts = tier.lower().split("-")
    if all(p in t for p in parts):  # “hd” / “hd-60s” 需要 hd 与 60s 都出现
        return True
    return parts[0] == "hd" and any(k in t for k in EXPLICIT_HD)


class Actions:
    """渠道无关的交互入口。reply(text, buttons=None) 把回复送回来源渠道。"""

    def __init__(self, studio: Studio, planner: Planner, worker: Worker | None = None):
        self.studio, self.planner, self.worker = studio, planner, worker
        self.store = studio.store
        self.push_round: Callable[[dict], None] | None = None  # 由 Daemon 注入（推送到 Telegram）
        self.jobs: queue.Queue[tuple] = queue.Queue()
        # 各渠道的主动发送（agent 在后台继续时用；控制台靠轮询对话历史，不需要推送）
        self.channel_send: dict[str, Reply] = {
            "web": lambda text, buttons=None: None,
            "cli": lambda text, buttons=None: None,
        }
        self.runtime = AgentRuntime(
            studio,
            planner,
            enqueue=lambda fn: self.jobs.put((fn, lambda *a, **k: None, False)),
            say_for=lambda ch: self._say(ch, self.channel_send.get(ch, self.channel_send["web"])),
            max_steps=getattr(studio.orch.cfg, "agent_max_steps", 16),
        )
        studio.decision_listeners.append(self.runtime.on_round_decided)

    # ---- 规划器队列（单线程，按需唤醒 LLM） ----
    def planner_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                item = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            job, reply = item[0], item[1]
            gated = item[2] if len(item) > 2 else True  # agent 的步骤自己把关（不阻塞队列）
            warned = False
            rm = self.studio.orch.resources
            while gated and not stop.is_set():  # LLM 推理前把关：资源不够就等渲染结束
                if rm.llm_admit()[0]:
                    break
                if not warned:
                    reply(tr("llm_queued_video"))
                    warned = True
                stop.wait(3)
            try:
                job()
            except (ParamError, StudioError, PlannerUnavailable, ValueError, KeyError) as e:
                reply(tr("cannot", e=e))
            except Exception as e:
                log.exception("Planner job failed")
                reply(tr("planner_error", e=e))

    def _say(self, channel: str, reply: Reply) -> Reply:
        def say(text: str, buttons=None) -> None:
            meta = {"buttons": buttons} if buttons else {}
            self.studio.log(channel, "out", text, **meta)  # 按钮也存下，控制台对话里可点
            reply(text, buttons)

        return say

    # ---- 入站文字 ----
    def inbound(
        self, channel: str, text: str, reply: Reply, attachments: list[dict] | None = None
    ) -> None:
        """attachments：已保存的上传 [{id, name}]（控制台上传或 Telegram 发图）。"""
        meta = {"attachments": attachments} if attachments else {}
        msg_id = self.studio.log(channel, "in", text, **meta)
        say = self._say(channel, reply)
        if text.startswith("/"):
            return self.command(channel, text, say)
        _, sid = self._ctx()
        if self.runtime.answer_pending_question(channel, sid, text):
            return  # 你在回答 agent 的提问，运行接着走
        pending = self.store.get_kv(f"pending:{channel}")
        if pending:
            self.store.set_kv(f"pending:{channel}", None)
            if pending["action"] == "refine":
                return self.refine_note(
                    pending["round"], pending["idx"], text, channel, say, msg_id
                )
            if pending["action"] == "reject":
                return self.reject_note(pending["round"], text, channel, say, msg_id)
        self.request(text, channel, say, msg_id, attachments)

    def _ctx(self) -> tuple[str, str]:
        pid = self.studio.current_project()["id"]
        return pid, self.studio.current_session(pid)["id"]

    def _offer_memories(self, texts: list[str], source: dict, say: Reply) -> None:
        for m in self.studio.propose_memories(texts, source):
            say(
                tr("mem_offer", t=m["text"]),
                [
                    [
                        (tr("mem_remember"), f"m:{m['id']}:accept"),
                        (tr("mem_ignore"), f"m:{m['id']}:ignore"),
                    ]
                ],
            )

    def request(
        self,
        text: str,
        channel: str,
        say: Reply,
        msg_id: int | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        if not self.planner.available():
            say(tr("command_only"))
            return
        say(tr("ack"))
        pid, sid = self._ctx()
        if self.store.get_kv("planner_style", "agent") == "agent":
            self.runtime.start(text, channel, pid, sid, attachments)
            return

        def job() -> None:
            plan = self.planner.plan_request(text, pid, sid, msg_id, attachments)
            self._offer_memories(plan.memories, {"message": msg_id}, say)
            ref = plan.round_ref and self.store.get_round(plan.round_ref)
            if plan.action == "select" and ref:
                rnd = self.studio.select(ref["id"], plan.pick or 1, role=plan.role, origin=channel)
                say(plan.reply + ("\n" + tr("locked_as", r=plan.role) if plan.role else ""))
                if not plan.role:
                    say(tr("ask_lock"), lock_buttons(rnd["decision"]["task_id"]))
                return
            if plan.action != "round":
                say(plan.reply)
                return
            parent = None
            if ref:  # 对某一轮的反馈：关闭该轮（打回 + 原话作批注）并连线
                parent = ref["id"]
                if ref["state"] == "review":
                    self.studio.reject(ref["id"], text, origin=channel)
            tpl = self.studio.orch.registry.template(plan.template)
            kind, tier, asked = tpl.kind, plan.tier, None
            approved = bool(tier) and explicit_tier(text, tier)  # 你在原话里点名了档位
            if (
                kind == "video"
                and tier in tpl.tiers
                and not tpl.tiers[tier].verified
                and not approved
            ):  # 视频的未实测档位可能超出内存预算：先用已实测档位出，未实测档位要你点按钮确认
                asked = tier
                tier = next((k for k, v in tpl.tiers.items() if v.verified), tpl.default_tier)
            rnd = self.studio.start_round(
                plan.template,
                plan.params,
                plan.n,
                tier,
                plan.title,
                origin=channel,
                request=text,
                parent_round=parent,
                project_id=pid,
                # 图片档位风险低，允许未实测档位；视频必须用已实测档位（内存）
                allow_unverified=kind == "image" or approved,
            )
            say(
                f"{plan.reply}\n"
                + tr("queued_round", n=rnd["n"], tpl=rnd["template"], tier=rnd["tier"])
            )
            if asked:
                say(
                    tr("tier_fallback", asked=asked, used=tier),
                    [[(tr("btn_try_tier", t=asked), f"u:{rnd['id']}:{asked}")]],
                )

        self.jobs.put((job, say))

    def refine_note(
        self, round_id: str, idx, note: str, channel: str, say: Reply, msg_id: int | None = None
    ) -> None:
        say(tr("refine_ack", idx=idx, note=note))
        rnd = self.store.get_round(round_id)
        task = next(t for t in self.store.round_tasks(round_id) if str(t["idx"]) == str(idx))
        _, sid = self._ctx()

        def job() -> None:
            delta, why, mems = self.planner.translate_note(
                rnd["template"], task["params"], note, rnd["project_id"], sid, msg_id
            )
            new = self.studio.refine(round_id, idx, delta, note, origin=channel)
            say(f"{why}\n" + tr("queued_refine", n=new["n"], idx=idx))
            self._offer_memories(mems, {"round": round_id, "message": msg_id}, say)

        self.jobs.put((job, say))

    def reject_note(
        self, round_id: str, note: str, channel: str, say: Reply, msg_id: int | None = None
    ) -> None:
        rnd = self.studio.reject(round_id, note, origin=channel)
        say(tr("reject_ack", note=note))
        # 记下“还欠一个按意见派生的新轮”：任务只在内存队列里，daemon 重启后据此补上
        rnd = self.store.update_round(round_id, decision={**rnd["decision"], "followup": "pending"})
        self.followup_note(rnd, note, channel, say, msg_id)

    def followup_note(
        self, rnd: dict, note: str, channel: str, say: Reply, msg_id: int | None = None
    ) -> None:
        _, sid = self._ctx()

        def done(state: str) -> None:
            cur = self.store.get_round(rnd["id"])
            self.store.update_round(rnd["id"], decision={**cur["decision"], "followup": state})

        def job() -> None:
            try:
                delta, why, mems = self.planner.translate_note(
                    rnd["template"], rnd["base_params"], note, rnd["project_id"], sid, msg_id
                )
            except Exception:
                done("failed")
                raise
            new = self.studio.start_round(
                rnd["template"],
                {**rnd["base_params"], **delta},
                rnd["n"],
                rnd["tier"],
                rnd["title"],
                origin=channel,
                request=note,
                parent_round=rnd["id"],
                project_id=rnd["project_id"],
                allow_unverified=True,
            )
            done("done")
            say(f"{why}\n" + tr("queued_new_round", n=new["n"]))
            self._offer_memories(mems, {"round": rnd["id"], "message": msg_id}, say)

        self.jobs.put((job, say))

    def reopen(self, round_id: str, channel: str, say: Reply) -> None:
        """更改审阅结果：轮回到待审阅，并重新推送候选。"""
        rnd, running = self.studio.reopen(round_id, origin=channel)
        extra = tr("reopen_children", n=running) if running else ""
        say(tr("reopened", t=rnd["title"] or rnd["template"], c=extra))
        if channel == "telegram" and self.push_round:
            self.push_round(rnd)

    def prewarm_current(self) -> None:
        """LLM 被视频卸载后重新加载：后台预热当前会话的前缀缓存。"""
        _, sid = self._ctx()
        if self.store.get_kv("planner_style", "agent") == "agent":
            self.jobs.put((lambda: self.runtime.prewarm(sid), lambda *a, **k: None, False))
        else:
            self.jobs.put((lambda: self.planner.prewarm(sid), lambda *a, **k: None))

    # ---- 按钮（Telegram callback_data 与 Web 共用格式） ----
    def button(self, channel: str, data: str, reply: Reply) -> bool:
        """返回 True 表示该消息上的按钮已失效、可清除。"""
        say = self._say(channel, reply)
        kind, _, rest = data.partition(":")
        try:
            if kind == "s":
                rid, idx = rest.split(":")
                rnd = self.studio.select(rid, idx, origin=channel)
                task_id = rnd["decision"]["task_id"]
                say(tr("selected_ask_lock", idx=idx), lock_buttons(task_id))
                return True
            if kind == "l":
                task_id, role = rest.split(":")
                pack = self.studio.lock(task_id, role)
                say(tr("pack_locked", v=pack["version"], r=role))
                return True
            if kind == "f":
                rid, idx = rest.split(":")
                self.store.set_kv(
                    f"pending:{channel}", {"action": "refine", "round": rid, "idx": idx}
                )
                say(tr("ask_refine_note", idx=idx))
                return False
            if kind == "x":
                self.store.set_kv(f"pending:{channel}", {"action": "reject", "round": rest})
                say(tr("ask_reject_note"))
                return False
            if kind == "r":
                new = self.studio.redo(rest, origin=channel)
                say(tr("redo_queued", n=new["n"]))
                return True
            if kind == "u":  # 你确认后才用未实测档位
                rid, tier = rest.split(":")
                src = self.store.get_round(rid)
                new = self.studio.start_round(
                    src["template"],
                    src["base_params"],
                    src["n"],
                    tier,
                    src["title"],
                    origin=channel,
                    request=f"tier {tier} (unmeasured)",
                    parent_round=rid,
                    project_id=src["project_id"],
                    allow_unverified=True,
                )
                say(tr("try_tier_queued", t=tier, n=new["n"]))
                return True
            if kind == "a":  # agent 的审批请求
                mid, yn = rest.split(":")
                say(self.runtime.on_approval(int(mid), yn in ("yes", "always"), yn == "always"))
                return True
            if kind == "o":
                self.reopen(rest, channel, say)
                return True
            if kind == "m":
                mid, act = rest.split(":")
                m = self.studio.set_memory(int(mid), act)
                say(tr("mem_accepted" if act == "accept" else "mem_ignored", t=m["text"]))
                return True
            if kind == "k":  # 确认删除会话
                out = self.studio.delete_session(rest)
                say(
                    tr(
                        "session_deleted",
                        t=out["title"],
                        m=out["messages"],
                        c=out["current"]["title"],
                    )
                )
                return True
            if kind == "noop":
                return True
        except StudioError as e:
            say(tr("not_done", e=e))
            return True
        say(tr("unknown_button", d=data))
        return False

    # ---- 命令 ----
    def command(self, channel: str, text: str, say: Reply) -> None:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        s, store = self.studio, self.store
        if cmd in ("/help", "/start"):
            say(i18n.help_text())
        elif cmd == "/status":
            say(self.status_text())
        elif cmd == "/fv":
            task = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if not task:
                say("/fv <task>")
                return
            pid, sid = self._ctx()
            try:
                self.runtime.delegate_freevideo(task, channel, pid, sid)
            except ValueError as e:
                say(str(e))
                return
            say(tr("fv_started"))
        elif cmd == "/pause":
            store.set_kv("paused", True)
            cur = self.worker.stop_current() if self.worker and args[:1] == ["now"] else None
            say(tr("paused") + (" " + tr("cancelled_running") if cur else ""))
        elif cmd == "/resume":
            store.set_kv("paused", False)
            say(tr("resumed"))
        elif cmd == "/cancel":
            n = s.cancel_queued(s.current_project()["id"])
            for r in store.runs(
                None, "running", "waiting_resources", "waiting_event", "waiting_user"
            ):
                if r["project_id"] == s.current_project()["id"]:
                    self.runtime.cancel(r["id"])
            pid = s.current_project()["id"]
            cur = self.worker and self.worker.stop_current(lambda t: t.get("project_id") == pid)
            say(tr("cancelled", n=n, x=tr("cancelled_running") if cur else ""))
        elif cmd == "/mode" and args and args[0] in ("simple", "agent"):
            store.set_kv("planner_style", args[0])
            say(tr("style_set", s=args[0]))
        elif cmd == "/mode":
            if not args:
                opts = ", ".join(f"{k} ({i18n.mode(k)})" for k in MODES)
                say(tr("mode_now", m=i18n.mode(self.planner.mode), opts=opts))
            else:
                self.planner.set_mode(args[0])
                say(tr("mode_set", m=i18n.mode(args[0])))
        elif cmd == "/model":
            from .planner import BRAINS

            opts = ", ".join(BRAINS)
            if not args:
                m = self.runtime.brain()
                off = m != "local" and not self.runtime.cfg.allow_online
                say(tr("model_now", m=m, opts=opts, note=tr("model_local_note") if off else ""))
            elif args[0] not in BRAINS:
                say(tr("model_unknown", m=args[0], opts=opts))
            elif args[0] != "local" and not self.runtime.cfg.allow_online:
                say(tr("agent_online_off"))
            else:
                store.set_kv("agent_brain", args[0])
                say(tr("model_set", m=args[0]))
        elif cmd in ("/permissions", "/perm"):
            rt = self.runtime
            if args and args[0] in ("default", "bypass"):
                store.set_kv("permission_mode", args[0])
                say(tr(f"perm_set_{args[0]}"))
            elif args and args[0] == "forget":
                rules = rt.allow_rules()
                drop = rules if args[1:] == ["all"] else [r for r in rules if r in args[1:]]
                store.set_kv("perm_allow", [r for r in rules if r not in drop])
                say(tr("perm_forgot", n=len(drop)))
            else:
                rules = "\n".join(f"· {r}" for r in rt.allow_rules()) or tr("perm_no_rules")
                say(tr("perm_now", m=rt.permission_mode(), rules=rules))
        elif cmd == "/projects":
            rows = [f"{i18n.state(p['state'])} · {p['name']}" for p in store.list_projects()]
            say("\n".join(rows) or tr("no_projects"))
        elif cmd == "/project":
            if not args:
                p = s.current_project()
                say(tr("project_now", n=p["name"], s=i18n.state(p["state"])))
            elif args[0] == "new" and len(args) > 1:
                say(tr("project_new", n=s.new_project(" ".join(args[1:]))["name"]))
            elif args[0] == "use" and len(args) > 1:
                say(tr("project_use", n=s.use_project(" ".join(args[1:]))["name"]))
            elif args[0] == "done":
                say(tr("project_done", n=s.finish_project()["name"]))
            else:
                say(tr("project_usage"))
        elif cmd == "/session":
            sub = args[0] if args else ""
            if sub == "new":
                say(tr("session_new", t=s.new_session(" ".join(args[1:]) or None)["title"]))
            elif sub == "use" and len(args) > 1:
                say(tr("session_use", t=s.use_session(args[1])["title"]))
            elif sub in ("delete", "del", "rm") and len(args) > 1:
                rows = store.sessions(s.current_project()["id"])
                key = args[1]
                row = (
                    rows[int(key) - 1]
                    if key.isdigit() and 0 < int(key) <= len(rows)
                    else store.get_session(key)
                )
                if row is None:
                    say(tr("not_done", e=f"no session {key}"))
                else:
                    say(
                        tr("session_delete_q", t=row["title"], n=row.get("n_messages", "?")),
                        [[(tr("btn_delete"), f"k:{row['id']}"), (tr("btn_keep"), "noop")]],
                    )
            else:
                cur = s.current_session()
                rows = store.sessions(s.current_project()["id"])
                lines = "\n".join(
                    tr(
                        "session_row",
                        i=i,
                        t=r["title"],
                        n=r["n_messages"],
                        cur=tr("current_mark") if r["id"] == cur["id"] else "",
                    )
                    for i, r in enumerate(rows, 1)
                )
                say(tr("session_list", rows=lines))
        elif cmd == "/memory":
            pid = s.current_project()["id"]
            if args and args[0] == "add" and len(args) > 1:
                m = s.add_memory(" ".join(args[1:]))
                say(tr("mem_added", id=m["id"], t=m["text"]))
            elif args and args[0] in ("del", "archive") and len(args) > 1:
                m = s.set_memory(int(args[1]), "archive")
                say(tr("mem_archived", id=m["id"], t=m["text"]))
            else:
                act = store.memories(pid, "active")
                prop = store.memories(pid, "proposed")
                items = "\n".join(f"#{m['id']} {m['text']}" for m in act) or tr("empty")
                pending = tr("mem_pending", n=len(prop)) if prop else ""
                say(tr("mem_list", items=items, pending=pending))
        elif cmd == "/pack":
            pack = store.pack(s.current_project()["id"])
            if not pack:
                say(tr("no_pack"))
            else:
                items = ", ".join(
                    f"{r} ({a['kind']}, seed {a.get('seed')})" for r, a in pack["assets"].items()
                )
                say(tr("pack_info", v=pack["version"], items=items))
        elif cmd == "/rounds":
            rows = store.rounds(s.current_project()["id"])[:6]
            say(
                "\n".join(
                    f"{i18n.state(r['state'])} · {r['title'] or r['template']} × {r['n']}"
                    for r in rows
                )
                or tr("no_rounds")
            )
        elif cmd == "/review":
            rows = store.rounds(s.current_project()["id"], "review")
            if not rows:
                say(tr("no_review"))
            elif not self.push_round:
                say(tr("review_no_tg", n=len(rows)))
            else:
                for r in reversed(rows):
                    self.push_round(r)
                say(tr("review_pushed", n=len(rows)))
        elif cmd == "/reopen":
            rows = [
                r
                for r in store.rounds(s.current_project()["id"], "closed")
                if (r["decision"] or {}).get("action") != "cancel"
            ]
            if not rows:
                say(tr("no_rounds"))
            else:
                self.reopen(args[0] if args else rows[0]["id"], channel, say)
        elif cmd == "/redo":
            rows = store.rounds(s.current_project()["id"], "review")
            if not rows:
                say(tr("no_review"))
            else:
                say(tr("redo_queued", n=s.redo(rows[0]["id"], origin=channel)["n"]))
        elif cmd == "/round":
            if not args:
                say(tr("round_usage", t=", ".join(s.orch.registry.templates)))
                return
            template, rest = args[0], args[1:]
            n = int(rest.pop(0)) if rest and rest[0].isdigit() else None
            params = {"prompt": " ".join(rest)} if rest else {}
            try:
                rnd = s.start_round(template, params, n, origin=channel)
            except (ParamError, KeyError, ValueError) as e:
                say(tr("invalid_params", e=e))
                return
            say(tr("queued_round", n=rnd["n"], tpl=rnd["template"], tier=rnd["tier"]))
        else:
            say(tr("unknown_cmd"))

    def status_text(self) -> str:
        m = memory.snapshot()
        p = self.studio.current_project()
        cur = self.worker and self.worker.current
        running = (
            tr("status_running", t=cur["title"] or cur["template"]) if cur else tr("status_idle")
        )
        return (
            tr(
                "status",
                p=p["name"],
                r=running,
                paused=tr("status_paused") if self.store.get_kv("paused") else "",
                q=len(self.store.by_state("draft")),
                m=m["avail_gib"],
                pr=m["pressure"],
                llm=i18n.mode(self.planner.mode) + self._llm_state(),
            )
            + self._status_extra()
        )

    def _llm_state(self) -> str:
        try:
            loaded = self.studio.orch.resources.llm.loaded()
        except Exception:  # noqa: BLE001 — LM Studio 不在线：不影响 /status
            return ""
        return tr("status_llm_loaded") if loaded else tr("status_llm_unloaded")

    def _status_extra(self) -> str:
        """不需要 LLM 的进度：agent 队列、拍片、进行中的制片。"""
        out = tr("status_bypass") if self.runtime.permission_mode() == "bypass" else ""
        active = len(self.store.runs(None, "running"))
        waiting = len(self.store.runs(None, "waiting_resources"))
        if active or waiting:
            out += tr("status_agent", a=active, w=waiting)
        films = self.studio.orch.resources.films
        if films:
            out += tr("status_films", n=len(films))
        prod = getattr(self, "production", None)
        for p in self.store.productions() if prod else []:
            if p["state"] != "active":
                continue
            try:
                stages = prod.plan(p["id"])["stages"]
            except Exception:  # 计划算不出来时跳过这一部，不影响 /status
                log.warning("status: plan for %s failed", p["id"], exc_info=True)
                continue
            cur = next((s for s in stages if s["state"] not in ("completed",)), None)
            if cur is None:
                continue
            c = ", ".join(f"{k} {v}" for k, v in (cur.get("counts") or {}).items() if v)
            n = len(cur.get("items") or [])
            out += tr(
                "status_prod",
                t=p["title"],
                stage=f"{cur['title']} [{cur['state']}]",
                c=f"{c} / {n}" if n else "-",
            )
        return out


class Daemon:
    def __init__(self, cfg: config.Settings):
        self.cfg = cfg
        i18n.set_language(cfg.language)
        self.stop = threading.Event()
        self.orch = Orchestrator(cfg, log=lambda m: log.info(m))
        self.studio = Studio(self.orch, cfg.round_size)
        self.planner = Planner(cfg, self.orch.store, self.orch.registry)
        self.planner.resources = self.orch.resources
        self.bot: TelegramBot | None = None
        self.worker = Worker(self.orch, self.studio, self.notify_round, self.stop)
        self.actions = Actions(self.studio, self.planner, self.worker)
        self.orch.on_llm_restored = self.actions.prewarm_current
        fv = None
        if cfg.freevideo_agent:
            from .freevideo import FreeVideo

            fv = FreeVideo(cfg.comfy_url)
        self.production = Production(
            self.studio,
            SkillLibrary.from_settings(cfg),
            load_workflows(cfg.workflows_dir, cfg.data_dir / "workflows"),
            self.skill_llm,
            log=lambda m: log.info(m),
            freevideo=fv,
        )
        if self.production.film is not None:
            self.production.film.notify_round = self.notify_round  # 导入的一轮 take：推送审阅
            self.production.film.notify_text = self.notify_film
            self.production.film.resources = self.orch.resources  # 拍片独占内存：卸载 / 拦住 LLM
        self.production.listeners.append(self.notify_stage)
        self.orch.resources.listeners.append(self.production.wake)  # 渲染结束 → 排队的 skill 再试
        self.actions.production = self.production
        self.actions.runtime.production = self.production
        from .comfy_bridge import ComfyBridge

        bridge = ComfyBridge(cfg)
        self.actions.runtime.comfy = bridge if bridge.available() else None
        self.worker.hold = self.production.holds
        self.production.stop_running = lambda pid: self.worker.stop_current(
            lambda t: self.production.task_production(t) == pid
        )
        if cfg.telegram_token:
            self.bot = TelegramBot(
                TelegramAPI(cfg.telegram_token), self.actions, cfg.telegram_chat_ids
            )
            self.actions.push_round = lambda r: self.bot.send_round(
                r, self.orch.store.round_tasks(r["id"]), self.orch.store.task_dir
            )
            self.actions.channel_send["telegram"] = self.bot.broadcast
            default_send = self.actions.runtime.send_files

            def send_files(ch: str, items: list) -> None:
                if ch != "telegram":
                    return default_send(ch, items)
                self.bot.send_files(items)  # 也记进当前会话，控制台的对话历史里看得到
                self.studio.log("telegram", "out", "\n".join(f"📎 {cap}: {p}" for p, cap in items))

            self.actions.runtime.send_files = send_files

    def notify_round(self, rnd: dict) -> None:
        tasks = self.orch.store.round_tasks(rnd["id"])
        ok = sum(1 for t in tasks if t["outputs"])
        self.studio.log(
            "system",
            "out",
            tr("round_ready", t=rnd["title"] or rnd["template"], ok=ok, n=len(tasks)),
            rnd["project_id"],
            round_id=rnd["id"],
        )
        if self.bot:
            self.bot.send_round(rnd, tasks, self.orch.store.task_dir)
        self.actions.runtime.on_round_ready(rnd)  # 在等这轮的 agent 运行带着结果继续
        self.production.on_round_ready(rnd)  # 制片阶段：自动通过 / 通知审阅

    # ---- 制片：skill 阶段的 LLM（本地优先；不够时按回退链；在线须 allow_online） ----
    def skill_llm(self, stage, messages: list[dict], schema: dict) -> tuple[str, dict]:
        from .agent_cli import AgentCLI
        from .planner import make_named_backend

        cfg, rm = self.cfg, self.orch.resources
        from .planner import AGENT_CLIS, ONLINE_APIS

        online = (*ONLINE_APIS, *AGENT_CLIS)

        def run(name: str) -> tuple[str, dict]:
            if name in ("claude_code", "codex"):
                prompt = "\n\n".join(m["content"] for m in messages)
                return AgentCLI(name, cfg).run(
                    f"skill-{stage.id}-{int(time.time())}",
                    prompt,
                    f"http://127.0.0.1:{cfg.web_port}",
                )
            res = make_named_backend(cfg, self.orch.store, name).chat(messages, schema, 8192)
            return res.text, res.stats()

        if stage.llm:  # 阶段指定了后端
            if stage.llm in online and not cfg.allow_online:
                raise Park(f"stage {stage.id} uses {stage.llm} but allow_online is off")
            if stage.llm in online:
                return run(stage.llm)
        primary = self.planner.backend
        if not getattr(primary, "local", False):
            res = primary.chat(messages, schema, 8192)
            return res.text, res.stats()
        ok, why = rm.llm_admit()
        if ok:
            with rm.llm.inference():
                res = primary.chat(messages, schema, 8192)
            return res.text, res.stats()
        if cfg.allow_online:
            for name in cfg.llm_fallback or []:
                if name in online:
                    log.info("skill %s: local LLM unavailable (%s) → %s", stage.id, why, name)
                    return run(name)
        raise Park(why)

    def notify_film(self, prod: dict, key: str) -> None:
        text = tr(key, p=prod["title"])
        self.studio.log("system", "out", text, prod["project_id"])
        if self.bot:
            self.bot.broadcast(text)

    def notify_stage(self, ev: dict) -> None:
        prod = ev["production"]
        if ev["status"] not in ("review", "invalid", "failed"):
            return
        key = f" · {ev['key']}" if ev["key"] != "main" else ""
        text = tr(f"stage_{ev['status']}", p=prod["title"], s=ev["title"] + key)
        self.studio.log("system", "out", text, prod["project_id"])
        if self.bot and not ev["artifact"].get("round_id"):  # 渲染阶段的轮已经单独推送过
            self.bot.broadcast(text)

    def resume_followups(self) -> int:
        """重启前没做完的“按打回意见派生新轮”：重新排队（只认带 pending 标记的，旧记录不动）。"""
        n = 0
        for r in self.orch.store.rounds(None, "closed"):
            d = r.get("decision") or {}
            if d.get("action") == "reject" and d.get("note") and d.get("followup") == "pending":
                ch = d.get("origin") or "web"
                say = self.actions._say(
                    ch, self.actions.channel_send.get(ch, self.actions.channel_send["web"])
                )
                self.actions.followup_note(r, d["note"], ch, say)
                n += 1
        if n:
            log.info("Re-queued %d reject note(s) interrupted by a restart", n)
        return n

    def start(self) -> None:
        self.actions.runtime.recover()
        self.resume_followups()
        self.production.recover()
        threading.Thread(
            target=self.production.work, args=(self.stop,), name="production", daemon=True
        ).start()
        self.worker.start()
        threading.Thread(target=self.worker.heartbeat, name="heartbeat", daemon=True).start()
        threading.Thread(
            target=self.actions.planner_loop, args=(self.stop,), name="planner", daemon=True
        ).start()
        if self.bot:
            threading.Thread(
                target=self.bot.poll_forever, args=(self.stop,), name="telegram", daemon=True
            ).start()
            log.info("Telegram listener started (whitelist %s)", self.cfg.telegram_chat_ids)
        else:
            log.info("No telegram_token configured; starting worker and web only")

    def shutdown(self) -> None:
        self.stop.set()
