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

from . import config, memory
from .comfy import ComfyBusy
from .planner import MODES, Planner, PlannerUnavailable
from .render import ParamError
from .scheduler import Orchestrator, SchedulerBusy
from .store import PROJECT_ZH, ROUND_ZH
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
                log.info("恢复 %s：%s", tid, outcome)
        except SchedulerBusy as e:
            log.warning("启动对账跳过：%s", e)
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
                log.info("另一个编排器进程在运行（例如 CLI），稍后重试")
                self.stop.wait(5)
                continue
            except Exception:
                log.exception("执行 %s 出错", task["id"])
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
                    reply("视频生成占着内存，LLM 先排队，生成结束后马上处理。")
                    warned = True
                stop.wait(3)
            try:
                job()
            except (ParamError, StudioError, PlannerUnavailable) as e:
                reply(f"没能处理：{e}")
            except Exception as e:
                log.exception("规划任务失败")
                reply(f"规划器出错：{e}")

    def _say(self, channel: str, reply: Reply) -> Reply:
        def say(text: str, buttons=None) -> None:
            self.studio.log(channel, "out", text)
            reply(text, buttons)

        return say

    # ---- 入站文字 ----
    def inbound(self, channel: str, text: str, reply: Reply) -> None:
        self.studio.log(channel, "in", text)
        say = self._say(channel, reply)
        if text.startswith("/"):
            return self.command(channel, text, say)
        pending = self.store.get_kv(f"pending:{channel}")
        if pending:
            self.store.set_kv(f"pending:{channel}", None)
            if pending["action"] == "refine":
                return self.refine_note(pending["round"], pending["idx"], text, channel, say)
            if pending["action"] == "reject":
                return self.reject_note(pending["round"], text, channel, say)
        self.request(text, channel, say)

    def request(self, text: str, channel: str, say: Reply) -> None:
        if self.planner.mode == "command_only":
            say("当前是纯命令模式，文字不会唤醒 LLM。用 /round 直接出图，或 /mode on_demand。")
            return
        say("收到，正在理解（LLM 冷启动约 15 秒）……")
        pid = self.studio.current_project()["id"]

        def job() -> None:
            plan = self.planner.plan_request(text, pid)
            if plan.action != "round":
                say(plan.reply)
                return
            rnd = self.studio.start_round(
                plan.template,
                plan.params,
                plan.n,
                plan.tier,
                plan.title,
                origin=channel,
                request=text,
                project_id=pid,
            )
            say(f"{plan.reply}\n已排队：{rnd['n']} 个候选（{rnd['template']} / {rnd['tier']}）")

        self.jobs.put((job, say))

    def refine_note(self, round_id: str, idx, note: str, channel: str, say: Reply) -> None:
        say(f"收到微调 #{idx}：{note}。正在把批注翻译成参数……")
        rnd = self.store.get_round(round_id)
        task = next(t for t in self.store.round_tasks(round_id) if str(t["idx"]) == str(idx))

        def job() -> None:
            delta, why = self.planner.translate_note(
                rnd["template"], task["params"], note, rnd["project_id"]
            )
            new = self.studio.refine(round_id, idx, delta, note, origin=channel)
            say(f"{why}\n已排队新一轮：{new['n']} 个候选（保留 #{idx} 的种子）")

        self.jobs.put((job, say))

    def reject_note(self, round_id: str, note: str, channel: str, say: Reply) -> None:
        rnd = self.studio.reject(round_id, note, origin=channel)
        say(f"已打回。正在按批注调整：{note}")

        def job() -> None:
            delta, why = self.planner.translate_note(
                rnd["template"], rnd["base_params"], note, rnd["project_id"]
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
            say(f"{why}\n已排队新一轮：{new['n']} 个候选")

        self.jobs.put((job, say))

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
                say(f"已选中 #{idx}。要锁进基准包吗？", lock_buttons(task_id))
                return True
            if kind == "l":
                task_id, role = rest.split(":")
                pack = self.studio.lock(task_id, role)
                say(f"基准包 v{pack['version']}：已锁定「{role}」。之后可用 @pack:{role} 引用。")
                return True
            if kind == "f":
                rid, idx = rest.split(":")
                self.store.set_kv(
                    f"pending:{channel}", {"action": "refine", "round": rid, "idx": idx}
                )
                say(f"请发一句对 #{idx} 的微调说明（例如：眼神更忧伤，光打侧面）")
                return False
            if kind == "x":
                self.store.set_kv(f"pending:{channel}", {"action": "reject", "round": rest})
                say("请发一句打回原因 / 改进方向")
                return False
            if kind == "r":
                new = self.studio.redo(rest, origin=channel)
                say(f"整轮重做：已排队 {new['n']} 个新候选")
                return True
            if kind == "noop":
                return True
        except StudioError as e:
            say(f"操作没有执行：{e}")
            return True
        say(f"未知按钮 {data}")
        return False

    # ---- 命令 ----
    def command(self, channel: str, text: str, say: Reply) -> None:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        s, store = self.studio, self.store
        if cmd in ("/help", "/start"):
            from .telegram import HELP

            say(HELP)
        elif cmd == "/status":
            say(self.status_text())
        elif cmd == "/pause":
            store.set_kv("paused", True)
            say("已暂停：正在跑的任务会跑完，排队的不再开始。/resume 恢复。")
        elif cmd == "/resume":
            store.set_kv("paused", False)
            say("已恢复出图。")
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
            say(f"已取消 {n} 个排队任务" + ("，并中断了正在跑的本系统任务" if cur else ""))
        elif cmd == "/mode":
            if not args:
                say(
                    "当前模式："
                    + MODES[self.planner.mode]
                    + "\n可选："
                    + "，".join(f"{k}（{v}）" for k, v in MODES.items())
                )
            else:
                self.planner.set_mode(args[0])
                say("已切换到：" + MODES[args[0]])
        elif cmd == "/projects":
            say(
                "\n".join(f"{PROJECT_ZH[p['state']]} · {p['name']}" for p in store.list_projects())
                or "还没有项目"
            )
        elif cmd == "/project":
            if not args:
                p = s.current_project()
                say(f"当前项目：{p['name']}（{PROJECT_ZH[p['state']]}）")
            elif args[0] == "new" and len(args) > 1:
                say(f"已新建并切换到「{s.new_project(' '.join(args[1:]))['name']}」")
            elif args[0] == "use" and len(args) > 1:
                say(f"已切换到「{s.use_project(' '.join(args[1:]))['name']}」")
            elif args[0] == "done":
                say(f"项目「{s.finish_project()['name']}」已标记完成，历史永久保留。")
            else:
                say("用法：/project [new 名称 | use 名称 | done]")
        elif cmd == "/pack":
            pack = store.pack(s.current_project()["id"])
            if not pack:
                say("当前项目还没有基准包。选中候选后可锁定。")
            else:
                say(
                    f"基准包 v{pack['version']}："
                    + "，".join(
                        f"{r}（{a['kind']}，seed {a.get('seed')}）"
                        for r, a in pack["assets"].items()
                    )
                )
        elif cmd == "/rounds":
            rows = store.rounds(s.current_project()["id"])[:6]
            say(
                "\n".join(
                    f"{ROUND_ZH[r['state']]} · {r['title'] or r['template']} × {r['n']}"
                    for r in rows
                )
                or "当前项目还没有轮次"
            )
        elif cmd == "/redo":
            rows = store.rounds(s.current_project()["id"], "review")
            if not rows:
                say("没有待审阅的轮次")
            else:
                new = s.redo(rows[0]["id"], origin=channel)
                say(f"整轮重做：已排队 {new['n']} 个新候选")
        elif cmd == "/round":
            if not args:
                say(
                    "用法：/round 模板 [数量] [prompt]；模板："
                    + "，".join(s.orch.registry.templates)
                )
                return
            template, rest = args[0], args[1:]
            n = int(rest.pop(0)) if rest and rest[0].isdigit() else None
            params = {"prompt": " ".join(rest)} if rest else {}
            try:
                rnd = s.start_round(template, params, n, origin=channel)
            except (ParamError, KeyError, ValueError) as e:
                say(f"参数无效：{e}")
                return
            say(f"已排队：{rnd['n']} 个候选（{rnd['template']} / {rnd['tier']}）")
        else:
            say("不认识这个命令。/help 查看全部命令。")

    def status_text(self) -> str:
        m = memory.snapshot()
        p = self.studio.current_project()
        queued = len(self.store.by_state("draft"))
        cur = self.worker and self.worker.current
        paused = "（已暂停）" if self.store.get_kv("paused") else ""
        return (
            f"项目：{p['name']}\n"
            f"出图：{'正在跑 ' + (cur['title'] or cur['template']) if cur else '空闲'}{paused}"
            f"，排队 {queued}\n"
            f"内存：可用 {m['avail_gib']} GiB，压力 {m['pressure']}\n"
            f"LLM：{MODES[self.planner.mode]}"
        )


class Daemon:
    def __init__(self, cfg: config.Settings):
        self.cfg = cfg
        self.stop = threading.Event()
        self.orch = Orchestrator(cfg, log=lambda m: log.info(m))
        self.studio = Studio(self.orch, cfg.round_size)
        self.planner = Planner(cfg, self.orch.store, self.orch.registry)
        self.bot: TelegramBot | None = None
        self.worker = Worker(self.orch, self.studio, self.notify_round, self.stop)
        self.actions = Actions(self.studio, self.planner, self.worker)
        if cfg.telegram_token:
            self.bot = TelegramBot(
                TelegramAPI(cfg.telegram_token), self.actions, cfg.telegram_chat_ids
            )

    def notify_round(self, rnd: dict) -> None:
        tasks = self.orch.store.round_tasks(rnd["id"])
        ok = sum(1 for t in tasks if t["outputs"])
        self.studio.log(
            "system",
            "out",
            f"「{rnd['title'] or rnd['template']}」{ok}/{len(tasks)} 个候选待审阅",
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
            log.info("Telegram 监听已启动（白名单 %s）", self.cfg.telegram_chat_ids)
        else:
            log.info("未配置 telegram_token，只启动 Worker 与 Web")

    def shutdown(self) -> None:
        self.stop.set()
