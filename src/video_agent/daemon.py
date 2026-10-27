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
from .comfy import ComfyBusy
from .i18n import tr
from .planner import MODES, Planner, PlannerUnavailable
from .render import ParamError
from .scheduler import Orchestrator, SchedulerBusy
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

    def run(self) -> None:
        try:
            for tid, outcome in self.orch.recover():
                log.info("Recovered %s: %s", tid, outcome)
        except SchedulerBusy as e:
            log.warning("Startup reconcile skipped: %s", e)
        self._refresh_rounds()
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
            if self.store.get_kv("paused"):
                self.stop.wait(2)
                continue
            task = self.store.next_draft()
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


class Actions:
    """渠道无关的交互入口。reply(text, buttons=None) 把回复送回来源渠道。"""

    def __init__(self, studio: Studio, planner: Planner, worker: Worker | None = None):
        self.studio, self.planner, self.worker = studio, planner, worker
        self.store = studio.store
        self.push_round: Callable[[dict], None] | None = None  # 由 Daemon 注入（推送到 Telegram）
        self.jobs: queue.Queue[tuple[Callable[[], None], Reply]] = queue.Queue()

    # ---- 规划器队列（单线程，按需唤醒 LLM） ----
    def planner_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                job, reply = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            warned = False
            while self.worker and self.worker.busy_kind == "video" and not stop.is_set():
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
            if kind == "video" and tier in tpl.tiers and not tpl.tiers[tier].verified:
                # 视频的未实测档位可能超出内存预算：先用已实测档位出，未实测档位要你点按钮确认
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
                allow_unverified=kind == "image",
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
        _, sid = self._ctx()

        def job() -> None:
            delta, why, mems = self.planner.translate_note(
                rnd["template"], rnd["base_params"], note, rnd["project_id"], sid, msg_id
            )
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
            say(f"{why}\n" + tr("queued_new_round", n=new["n"]))
            self._offer_memories(mems, {"round": round_id, "message": msg_id}, say)

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
            if kind == "o":
                self.reopen(rest, channel, say)
                return True
            if kind == "m":
                mid, act = rest.split(":")
                m = self.studio.set_memory(int(mid), act)
                say(tr("mem_accepted" if act == "accept" else "mem_ignored", t=m["text"]))
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
        elif cmd == "/pause":
            store.set_kv("paused", True)
            say(tr("paused"))
        elif cmd == "/resume":
            store.set_kv("paused", False)
            say(tr("resumed"))
        elif cmd == "/cancel":
            n = s.cancel_queued(s.current_project()["id"])
            cur = self.worker and self.worker.current
            if cur and cur.get("project_id") == s.current_project()["id"]:
                t = store.get(cur["id"])
                if t["prompt_id"]:
                    try:
                        s.orch.comfy.interrupt(t["prompt_id"])
                    except (ComfyBusy, OSError):
                        pass
            say(tr("cancelled", n=n, x=tr("cancelled_running") if cur else ""))
        elif cmd == "/mode":
            if not args:
                opts = ", ".join(f"{k} ({i18n.mode(k)})" for k in MODES)
                say(tr("mode_now", m=i18n.mode(self.planner.mode), opts=opts))
            else:
                self.planner.set_mode(args[0])
                say(tr("mode_set", m=i18n.mode(args[0])))
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
        return tr(
            "status",
            p=p["name"],
            r=running,
            paused=tr("status_paused") if self.store.get_kv("paused") else "",
            q=len(self.store.by_state("draft")),
            m=m["avail_gib"],
            pr=m["pressure"],
            llm=i18n.mode(self.planner.mode),
        )


class Daemon:
    def __init__(self, cfg: config.Settings):
        self.cfg = cfg
        i18n.set_language(cfg.language)
        self.stop = threading.Event()
        self.orch = Orchestrator(cfg, log=lambda m: log.info(m))
        self.studio = Studio(self.orch, cfg.round_size)
        self.planner = Planner(cfg, self.orch.store, self.orch.registry)
        self.bot: TelegramBot | None = None
        self.worker = Worker(self.orch, self.studio, self.notify_round, self.stop)
        self.actions = Actions(self.studio, self.planner, self.worker)
        self.orch.on_llm_restored = self.actions.prewarm_current
        if cfg.telegram_token:
            self.bot = TelegramBot(
                TelegramAPI(cfg.telegram_token), self.actions, cfg.telegram_chat_ids
            )
            self.actions.push_round = lambda r: self.bot.send_round(
                r, self.orch.store.round_tasks(r["id"]), self.orch.store.task_dir
            )

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

    def start(self) -> None:
        self.worker.start()
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
