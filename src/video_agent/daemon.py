"""Long-running service `va daemon`: Worker (serial rendering) + planner queue (wakes the LLM on demand) + Telegram listener + web.

- The Worker executes draft tasks only through the orchestrator; when a whole round of candidates is done, review is notified.
- Actions is the channel-independent entry: shared by Telegram and the web console; every message goes into chat history.
- Commands and buttons run immediately; free text goes to the planner queue. While a video render holds memory the planner waits, never preempts.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable

import httpx

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
        )  # True: do not run it yet (e.g. its production is paused)

    def _recover(self) -> bool:
        """Startup reconcile. False when ComfyUI is unreachable: the caller retries
        instead of letting the worker thread die (the daemon may start before ComfyUI)."""
        try:
            for tid, outcome in self.orch.recover():
                log.info("Recovered %s: %s", tid, outcome)
        except SchedulerBusy as e:
            log.warning("Startup reconcile skipped: %s", e)
        except (httpx.HTTPError, OSError) as e:
            log.warning("Startup reconcile failed (ComfyUI offline?): %s; retrying in 30 s", e)
            return False
        return True

    def _reconcile_timeouts(self) -> None:
        try:
            collected = self.orch.reconcile_timeouts()
        except (httpx.HTTPError, OSError) as e:
            log.warning("Timeout reconcile failed (ComfyUI offline?): %s", e)
            return
        for tid, state in collected:
            log.info("Collected %s after timeout: %s", tid, state)
            t = self.store.get(tid)
            if t["round_id"]:
                rnd, ready = self.studio.refresh_round(t["round_id"])
                if ready:
                    self.notify(rnd)

    def run(self) -> None:
        # queued work waits until the startup reconcile has gone through
        while not self._recover():
            if self.stop.wait(30):
                return
        self._refresh_rounds()
        last_reconcile = 0.0
        while not self.stop.is_set():
            if self.store.get_kv("paused"):
                self.stop.wait(2)
                continue
            # periodically collect tasks that finished after their watch timed out
            if time.monotonic() - last_reconcile > 120:
                last_reconcile = time.monotonic()
                self._reconcile_timeouts()
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
        """Interrupt the running task (only the job we submitted); returns the interrupted task."""
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
        """Heartbeat in its own thread: while rendering, the Worker is stuck in one long task and the heartbeat must not stop with it."""
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
    """You named the tier explicitly in your message (e.g. hd, 高清, 10s): counts as approval to use an unmeasured tier."""
    t = text.lower()
    parts = tier.lower().split("-")
    if all(p in t for p in parts):  # "hd" / "hd-60s" needs both hd and 60s to appear
        return True
    return parts[0] == "hd" and any(k in t for k in EXPLICIT_HD)


class Actions:
    """Channel-independent interaction entry. reply(text, buttons=None) sends the answer back to the source channel."""

    def __init__(self, studio: Studio, planner: Planner, worker: Worker | None = None):
        self.studio, self.planner, self.worker = studio, planner, worker
        self.store = studio.store
        from .llm_control import LLMControl

        self.llm_control = LLMControl(studio.orch.cfg, studio.orch.resources, planner)
        # injected by Daemon (push to Telegram)
        self.push_round: Callable[[dict], None] | None = None
        self.jobs: queue.Queue[tuple] = queue.Queue()
        # proactive sends per channel (used when the agent continues in the background; the console
        # polls chat history, no push needed)
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
        self.runtime.llm_control = self.llm_control

    # ---- planner queue (single thread, wakes the LLM on demand) ----
    def planner_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                item = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            job, reply = item[0], item[1]
            # agent steps gate themselves (they do not block the queue)
            gated = item[2] if len(item) > 2 else True
            warned = False
            rm = self.studio.orch.resources
            # gate before LLM inference: without resources, wait for the render to end
            while gated and not stop.is_set():
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
            # buttons are stored too, so they are clickable in the console chat
            self.studio.log(channel, "out", text, **meta)
            reply(text, buttons)

        return say

    # ---- inbound text ----
    def inbound(
        self, channel: str, text: str, reply: Reply, attachments: list[dict] | None = None
    ) -> None:
        """attachments: saved uploads [{id, name}] (console upload or an image sent in Telegram)."""
        meta = {"attachments": attachments} if attachments else {}
        msg_id = self.studio.log(channel, "in", text, **meta)
        say = self._say(channel, reply)
        if text.startswith("/"):
            return self.command(channel, text, say)
        _, sid = self._ctx()
        if self.runtime.answer_pending_question(channel, sid, text):
            return  # you are answering the agent's question; the run continues
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

    def _llm_command(self, args: list[str], say: Reply) -> None:
        """/llm · /llm use NAME|N · /llm load · /llm unload — executed directly, no LLM involved."""
        from .llm_control import LLMControlError

        ctl = self.llm_control
        sub = args[0] if args else ""
        try:
            if sub == "use" and len(args) > 1:
                out = ctl.use(" ".join(args[1:]))
            elif sub in ("load", "reload"):
                out = ctl.load(reload=sub == "reload")
            elif sub == "unload":
                out = ctl.unload()
                say(tr("llm_unloaded", m=out["model"]))
                return
            else:
                st = ctl.status()
                loaded = {m["id"] for m in st["loaded"]}
                rows = "\n".join(
                    f"{i}. {m['key']} · {round((m.get('size') or 0) / 2**30, 1)} GiB"
                    + (" · 👁" if m.get("vision") else "")
                    + (" ✅" if m["key"] in loaded else "")
                    + (tr("current_mark") if m["key"] == st["model"] else "")
                    for i, m in enumerate(st["models"], 1)
                )
                say(tr("llm_list", rows=rows or "-"))
                return
        except LLMControlError as e:
            say(tr("not_done", e=e))
            return
        if out["state"] != "working":
            say(tr("llm_already", m=out["model"]))
            return
        say(tr("llm_switching", m=out["model"], g=out.get("need_gib", "?")))

        def report() -> None:
            job = ctl.wait()
            if job["state"] == "error":
                say(tr("not_done", e=job["error"]))
            else:
                say(tr("llm_ready", m=out["model"], s=job.get("seconds", "?")))

        threading.Thread(target=report, daemon=True).start()

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
            # feedback on a round: close that round (reject + your words as the note) and link it
            if ref:
                parent = ref["id"]
                if ref["state"] == "review":
                    self.studio.reject(ref["id"], text, origin=channel)
            tpl = self.studio.orch.registry.template(plan.template)
            kind, tier, asked = tpl.kind, plan.tier, None
            # you named the tier in your message
            approved = bool(tier) and explicit_tier(text, tier)
            if (
                kind == "video"
                and tier in tpl.tiers
                and not tpl.tiers[tier].verified
                and not approved
                # an unmeasured video tier may exceed the memory budget: use a measured tier first; the
                # unmeasured one needs your button press
            ):
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
                # image tiers are low-risk, unmeasured ones allowed; video must use a measured tier
                # (memory)
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
        # record "a follow-up round from the note is still owed": the task only lives in the
        # in-memory queue, so a daemon restart can redo it from this
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
        """Change a review decision: the round goes back to review and its candidates are pushed again."""
        rnd, running = self.studio.reopen(round_id, origin=channel)
        extra = tr("reopen_children", n=running) if running else ""
        say(tr("reopened", t=rnd["title"] or rnd["template"], c=extra))
        if channel == "telegram" and self.push_round:
            self.push_round(rnd)

    def prewarm_current(self) -> None:
        """The LLM is reloaded after a video unloaded it: prewarm the current session's prefix cache in the background."""
        _, sid = self._ctx()
        if self.store.get_kv("planner_style", "agent") == "agent":
            self.jobs.put((lambda: self.runtime.prewarm(sid), lambda *a, **k: None, False))
        else:
            self.jobs.put((lambda: self.planner.prewarm(sid), lambda *a, **k: None))

    # ---- buttons (same format for Telegram callback_data and the web) ----
    def button(self, channel: str, data: str, reply: Reply) -> bool:
        """Returns True when the buttons on that message are now stale and can be cleared."""
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
            if kind == "u":  # use the unmeasured tier only after you confirm
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
            if kind == "a":  # an approval request from the agent
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
            if kind == "k":  # confirm deleting a session
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

    # ---- commands ----
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
        elif cmd == "/llm":
            self._llm_command(args, say)
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
        except Exception:  # noqa: BLE001 — LM Studio offline: does not affect /status
            return ""
        return tr("status_llm_loaded") if loaded else tr("status_llm_unloaded")

    def _status_extra(self) -> str:
        """Progress that needs no LLM: agent queue, film shoots, active productions."""
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
            # if the plan cannot be computed, skip this part; does not affect /status
            except Exception:
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
            # an imported round of takes: push for review
            self.production.film.notify_round = self.notify_round
            self.production.film.notify_text = self.notify_film
            # shooting holds the memory: unload / block the LLM
            self.production.film.resources = self.orch.resources
        self.production.listeners.append(self.notify_stage)
        # render finished → queued skills try again
        self.orch.resources.listeners.append(self.production.wake)
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
                # also logged into the current session so the console's chat history shows it
                self.bot.send_files(items)
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
        # agent runs waiting for this round continue with the result
        self.actions.runtime.on_round_ready(rnd)
        self.production.on_round_ready(rnd)  # production stages: auto-approve / notify for review

    # ---- productions: LLM for skill stages (local first; fallback chain when short of resources;
    # online needs allow_online) ----
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

        if stage.llm:  # the stage names a backend
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
        # render stages' rounds were already pushed separately
        if self.bot and not ev["artifact"].get("round_id"):
            self.bot.broadcast(text)

    def resume_followups(self) -> int:
        """Re-queue "follow-up rounds from reject notes" not finished before a restart (only ones marked pending; old records untouched)."""
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
