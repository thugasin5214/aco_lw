"""Telegram entry: plain-Python long polling, loads no model.

Commands and buttons are executed directly by the listener (deterministic); only free text (new requests, notes) goes to the planner queue and wakes the LLM.
Candidates are sent as files (uncompressed). Only whitelisted chat_ids get answers (except /whoami, used for first-time setup).
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
        """Send originals as files; more than one goes via sendMediaGroup (at most 10 per group)."""
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
        """Download an image / file the user sent; returns (filename, content)."""
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
    """Translate Telegram updates into Actions calls; all business logic lives in Actions / Studio."""

    def __init__(self, api: TelegramAPI, actions, allowed: list[int]):
        self.api = api
        self.actions = actions
        self.allowed = set(allowed)
        self.offset: int | None = None

    # ---- outbound ----
    def broadcast(self, text: str, buttons=None) -> None:
        for chat in self.allowed:
            try:
                self.api.send(chat, text, buttons)
            except Exception as e:  # noqa: BLE001 — a failed notice does not affect generation
                log.warning("Telegram send failed: %s", e)

    def send_files(self, items: list[tuple[Path, str]]) -> None:
        """The agent's send_file: send files (contact sheets, videos, ...) to you."""
        for chat in self.allowed:
            try:
                self.api.send_documents(chat, items)
            except Exception as e:  # noqa: BLE001 — if it cannot be sent, just log it
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

    # ---- inbound ----
    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                updates = self.api.get_updates(self.offset)
            except Exception as e:  # noqa: BLE001 — network hiccup: retry a bit later
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
            # button presses also go into the current session (so the console's chat history shows
            # what you pressed)
            try:
                self.actions.studio.log("telegram", "in", tr("btn_pressed", b=label), button=data)
            # a failed log entry does not affect the button itself
            except Exception as e:  # noqa: BLE001
                log.warning("Logging button press failed: %s", e)
            done = self.actions.button("telegram", data, reply)
            if done and msg:
                self.api.clear_buttons(chat, msg["message_id"])

    def _attachments(self, msg: dict) -> list[dict]:
        """Photos (largest size), videos, images / videos sent as files → saved as uploads for @upload references.
        Telegram bots can only download files ≤ 20 MB; send a link for anything larger."""
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
