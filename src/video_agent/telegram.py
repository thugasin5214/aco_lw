"""Telegram 入口：纯 Python 长轮询，不加载模型。

命令与按钮由监听器直接执行（确定性）；只有自由文本（新需求、批注）才交给规划器队列唤醒 LLM。
候选以文件方式发送（不压缩）。只响应白名单 chat_id（/whoami 例外，用于首次配置）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger("aco_lw.telegram")

HELP = """aco_lw 命令（不唤醒 LLM）：
/status 系统与队列状态
/rounds 最近几轮
/round 模板 [数量] [prompt]  直接出一轮候选
/redo 最近一轮待审阅的整轮重做
/pause /resume 暂停 / 恢复出图
/cancel 取消排队中的任务（含正在跑的本系统任务）
/mode [on_demand|session|always|command_only] LLM 驻留模式
/project 当前项目 · /projects 全部项目
/project new 名称 · /project use 名称 · /project done
/pack 当前基准包
/whoami 显示 chat_id
其它文字：新需求或批注，会唤醒 LLM 理解。"""


class TelegramAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=60, transport=transport
        )

    def call(self, method: str, files: dict | None = None, **params) -> dict:
        if files:
            data = {k: v if isinstance(v, str | int) else json.dumps(v) for k, v in params.items()}
            r = self.http.post(method, data=data, files=files, timeout=300)
        else:
            r = self.http.post(method, json=params)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{j.get('description')}")
        return j["result"]

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        r = self.http.post("getUpdates", json=params, timeout=timeout + 10)
        return r.json().get("result", [])

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None):
        params = {"chat_id": chat_id, "text": text[:4000]}
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": t, "callback_data": d} for t, d in row] for row in buttons
                ]
            }
        return self.call("sendMessage", **params)

    def send_documents(self, chat_id: int, items: list[tuple[Path, str]]) -> None:
        """原图以文件发送；多于 1 个用 sendMediaGroup（每组最多 10 个）。"""
        for i in range(0, len(items), 10):
            chunk = items[i : i + 10]
            if len(chunk) == 1:
                path, caption = chunk[0]
                with path.open("rb") as f:
                    self.call(
                        "sendDocument",
                        files={"document": (path.name, f)},
                        chat_id=chat_id,
                        caption=caption,
                    )
                continue
            files, media = {}, []
            for j, (path, caption) in enumerate(chunk):
                key = f"f{j}"
                files[key] = (path.name, path.read_bytes())
                media.append({"type": "document", "media": f"attach://{key}", "caption": caption})
            self.call("sendMediaGroup", files=files, chat_id=chat_id, media=media)

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except RuntimeError:
            pass

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except RuntimeError:
            pass


class TelegramBot:
    """把 Telegram 更新翻译成 Actions 调用；所有业务逻辑在 Actions / Studio 里。"""

    def __init__(self, api: TelegramAPI, actions, allowed: list[int]):
        self.api = api
        self.actions = actions
        self.allowed = set(allowed)
        self.offset: int | None = None

    # ---- 出站 ----
    def broadcast(self, text: str, buttons=None) -> None:
        for chat in self.allowed:
            try:
                self.api.send(chat, text, buttons)
            except Exception as e:  # noqa: BLE001 — 通知失败不影响出图
                log.warning("Telegram 发送失败：%s", e)

    def send_round(self, rnd: dict, tasks: list[dict], task_dir: Callable[[str], Path]) -> None:
        items = []
        for t in tasks:
            for o in t["outputs"][:1]:
                items.append((task_dir(t["id"]) / o["file"], f"#{t['idx']}"))
        ok = [t for t in tasks if t["outputs"]]
        title = rnd["title"] or rnd["template"]
        for chat in self.allowed:
            try:
                if items:
                    self.api.send_documents(chat, items)
                if not ok:
                    self.api.send(
                        chat,
                        f"「{title}」这一轮全部失败，请看控制台。",
                        [[("🎲 重做", f"r:{rnd['id']}")]],
                    )
                    continue
                self.api.send(chat, f"「{title}」{len(ok)} 个候选待审阅", round_buttons(rnd, ok))
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram 发送候选失败：%s", e)

    # ---- 入站 ----
    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                updates = self.api.get_updates(self.offset)
            except Exception as e:  # noqa: BLE001 — 网络抖动时稍后重试
                log.warning("getUpdates 失败：%s", e)
                stop.wait(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:
                    log.exception("处理更新失败")
                    chat = _chat_of(u)
                    if chat in self.allowed:
                        self.api.send(chat, f"出错了：{e}")

    def handle(self, u: dict) -> None:
        chat = _chat_of(u)
        if "message" in u:
            text = (u["message"].get("text") or "").strip()
            if text.startswith("/whoami"):
                self.api.send(chat, f"你的 chat_id：{chat}")
                return
            if chat not in self.allowed or not text:
                return
            reply = lambda m, b=None: self.api.send(chat, m, b)
            self.actions.inbound("telegram", text, reply)
            return
        if "callback_query" in u:
            cq = u["callback_query"]
            if chat not in self.allowed:
                return
            self.api.answer(cq["id"])
            msg = cq.get("message", {})
            reply = lambda m, b=None: self.api.send(chat, m, b)
            done = self.actions.button("telegram", cq.get("data", ""), reply)
            if done and msg:
                self.api.clear_buttons(chat, msg["message_id"])


def round_buttons(rnd: dict, tasks: list[dict]) -> list[list[tuple[str, str]]]:
    rid = rnd["id"]
    return [
        [(f"✅ 选{t['idx']}", f"s:{rid}:{t['idx']}") for t in tasks],
        [(f"🔧 微调{t['idx']}", f"f:{rid}:{t['idx']}") for t in tasks],
        [("🎲 整轮重做", f"r:{rid}"), ("↩️ 打回+批注", f"x:{rid}")],
    ]


def lock_buttons(task_id: str) -> list[list[tuple[str, str]]]:
    return [
        [
            ("🔒 角色", f"l:{task_id}:角色"),
            ("🔒 场景", f"l:{task_id}:场景"),
            ("🔒 风格", f"l:{task_id}:风格"),
        ],
        [("不锁定", "noop")],
    ]


def _chat_of(u: dict) -> int | None:
    if "message" in u:
        return u["message"]["chat"]["id"]
    if "callback_query" in u:
        return u["callback_query"].get("message", {}).get("chat", {}).get("id")
    return None
