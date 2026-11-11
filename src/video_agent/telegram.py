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

from .i18n import roles, tr

log = logging.getLogger("aco_lw.telegram")


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

    def download(self, file_id: str) -> tuple[str, bytes]:
        """下载用户发来的图片 / 文件，返回 (文件名, 内容)。"""
        info = self.call("getFile", file_id=file_id)
        path = info["file_path"]
        base = str(self.http.base_url).replace("/bot", "/file/bot", 1)
        r = self.http.get(base.rstrip("/") + "/" + path, timeout=120)
        r.raise_for_status()
        return path.rsplit("/", 1)[-1], r.content

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
                log.warning("Telegram send failed: %s", e)

    def send_files(self, items: list[tuple[Path, str]]) -> None:
        """agent 的 send_file：把文件（联系表、视频…）发给你。"""
        for chat in self.allowed:
            try:
                self.api.send_documents(chat, items)
            except Exception as e:  # noqa: BLE001 — 发不出去只记日志
                log.warning("Telegram send_files failed: %s", e)

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
                        tr("round_failed", t=title),
                        [[(tr("btn_redo"), f"r:{rnd['id']}")]],
                    )
                    continue
                self.api.send(
                    chat, tr("round_ready_tg", t=title, n=len(ok)), round_buttons(rnd, ok)
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram send (candidates) failed: %s", e)

    # ---- 入站 ----
    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                updates = self.api.get_updates(self.offset)
            except Exception as e:  # noqa: BLE001 — 网络抖动时稍后重试
                log.warning("getUpdates failed: %s", e)
                stop.wait(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:
                    log.exception("Handling update failed")
                    chat = _chat_of(u)
                    if chat in self.allowed:
                        self.api.send(chat, tr("error", e=e))

    def handle(self, u: dict) -> None:
        chat = _chat_of(u)
        if "message" in u:
            msg = u["message"]
            text = (msg.get("text") or msg.get("caption") or "").strip()
            if text.startswith("/whoami"):
                self.api.send(chat, tr("whoami", id=chat))
                return
            if chat not in self.allowed:
                log.info("Ignoring message from non-whitelisted chat_id=%s", chat)
                return
            attachments = self._attachments(msg)
            if not text and not attachments:
                return
            reply = lambda m, b=None: self.api.send(chat, m, b)
            self.actions.inbound("telegram", text or "(image)", reply, attachments or None)
            return
        if "callback_query" in u:
            cq = u["callback_query"]
            if chat not in self.allowed:
                return
            self.api.answer(cq["id"])
            msg = cq.get("message", {})
            reply = lambda m, b=None: self.api.send(chat, m, b)
            data = cq.get("data", "")
            label = next(
                (
                    b.get("text")
                    for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])
                    for b in row
                    if b.get("callback_data") == data
                ),
                data,
            )
            try:  # 按钮点击也记进当前会话（控制台的对话历史里看得到你点了什么）
                self.actions.studio.log("telegram", "in", tr("btn_pressed", b=label), button=data)
            except Exception as e:  # noqa: BLE001 — 记录失败不影响按钮本身
                log.warning("Logging button press failed: %s", e)
            done = self.actions.button("telegram", data, reply)
            if done and msg:
                self.api.clear_buttons(chat, msg["message_id"])

    def _attachments(self, msg: dict) -> list[dict]:
        """照片（取最大尺寸）、视频、以文件发送的图片 / 视频 → 保存为上传，供 @upload 引用。
        Telegram 机器人只能下载 ≤ 20 MB 的文件；更大的请发链接。"""
        file_id, name = None, None
        doc = msg.get("document") or {}
        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
        elif msg.get("video"):
            file_id = msg["video"]["file_id"]
            name = msg["video"].get("file_name") or f"{msg['video']['file_unique_id']}.mp4"
        elif doc.get("mime_type", "").startswith(("image/", "video/")):
            file_id, name = doc["file_id"], doc.get("file_name")
        if not file_id:
            return []
        fname, data = self.api.download(file_id)
        up = self.actions.studio.save_upload(name or fname, data)
        return [{"id": up["id"], "name": up["name"]}]


def round_buttons(rnd: dict, tasks: list[dict]) -> list[list[tuple[str, str]]]:
    rid = rnd["id"]
    return [
        [(tr("btn_select", i=t["idx"]), f"s:{rid}:{t['idx']}") for t in tasks],
        [(tr("btn_refine", i=t["idx"]), f"f:{rid}:{t['idx']}") for t in tasks],
        [(tr("btn_redo"), f"r:{rid}"), (tr("btn_reject"), f"x:{rid}")],
    ]


def lock_buttons(task_id: str) -> list[list[tuple[str, str]]]:
    return [[(f"🔒 {r}", f"l:{task_id}:{r}") for r in roles()], [(tr("btn_no_lock"), "noop")]]


def _chat_of(u: dict) -> int | None:
    if "message" in u:
        return u["message"]["chat"]["id"]
    if "callback_query" in u:
        return u["callback_query"].get("message", {}).get("chat", {}).get("id")
    return None
