"""FreeVideo agent API 客户端（本机 ComfyUI 上的 /freevideo/agent/v1）。

用来做视频一致性：角色卡（每张最多 6 张标注图 + 可选声音）与多镜头电影（每镜多条 take、
下一镜从上一镜选中的 take 续拍）。

- token 只在进程内从 FreeVideo-local/agent/token 读取；不打印、不写进别的文件、不进日志。
- 只做 API 允许外部 agent 做的事；暂停 / 继续电影只有用户能在 FreeVideo 页面操作。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_ROOT = Path("~/Documents/ChatGPT/jev/FreeVideo-local").expanduser()
CARD_LABELS = ("front", "three_quarter", "side", "back", "face", "sheet", "other")


class FreeVideoError(RuntimeError):
    def __init__(self, status: int, body: Any):
        err = body.get("error", {}) if isinstance(body, dict) else {}
        self.code = err.get("code", str(status))
        self.body = body
        msg = err.get("message") or str(body)[:300]
        nxt = err.get("next")
        super().__init__(f"FreeVideo {self.code}: {msg}" + (f" — {nxt}" if nxt else ""))


class FreeVideo:
    def __init__(
        self,
        comfy_url: str = "http://127.0.0.1:8188",
        root: Path = DEFAULT_ROOT,
        client_name: str = "aco_lw",
        http: httpx.Client | None = None,
    ):
        self.base = comfy_url.rstrip("/") + "/freevideo/agent/v1"
        self.root, self.client_name = root, client_name
        self.http = http or httpx.Client(timeout=120, trust_env=False)
        self._hello = False

    # ---- 传输 ----
    def _headers(self) -> dict:
        token = (self.root / "agent" / "token").read_text().strip()  # 每次读：用户可能重置 token
        return {"Authorization": "Bearer " + token}

    def call(self, method: str, path: str, body: Any = None, **params) -> Any:
        if not self._hello and path != "/connect/hello":
            self._hello = True
            try:
                self.call("POST", "/connect/hello", {"client": self.client_name, "via": "http"})
            except FreeVideoError:
                pass  # 打招呼只是让用户在 FreeVideo 页面看到是谁连着；失败不影响调用
        r = self.http.request(
            method, self.base + path, json=body, params=params or None, headers=self._headers()
        )
        try:
            data = r.json()
        except json.JSONDecodeError:
            data = r.text
        if r.status_code >= 400:
            raise FreeVideoError(r.status_code, data)
        return data

    def download(self, url: str, dest: Path) -> Path:
        """下载结果文件（url 可以是相对 ComfyUI 根的路径）。"""
        full = url if url.startswith("http") else self.base.split("/freevideo/")[0] + url
        with self.http.stream("GET", full, headers=self._headers()) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    # ---- 状态 ----
    def status(self) -> dict:
        return self.call("GET", "")

    # ---- 素材 ----
    def upload(self, path: str | Path) -> str:
        """上传图片 / 视频 / 音频，返回 FreeVideo 里的 media 名。"""
        p = Path(path)
        out = self.call(
            "POST",
            "/media",
            {"name": p.name, "data_base64": base64.b64encode(p.read_bytes()).decode()},
        )
        media = out.get("media") or out
        return media.get("file") or media.get("name") or media["id"]

    # ---- 角色卡 ----
    def cards(self, query: str | None = None) -> list[dict]:
        out = self.call("GET", "/cast", **({"q": query} if query else {}))
        return out.get("cards") or out.get("cast") or out.get("items") or []

    def card(self, card_id: str) -> dict:
        out = self.call("GET", f"/cast/{card_id}")
        return out.get("card") or out

    def create_card(
        self,
        name: str,
        description: str,
        images: list[tuple[str | Path, str]],
        kind: str = "character",
    ) -> dict:
        """images：[(本地路径, 标签)]，标签取 front / three_quarter / side / back / face / sheet / other。"""
        pics = []
        for path, label in images[:6]:
            if label not in CARD_LABELS:
                raise ValueError(f"unknown picture label {label}")
            pics.append({"media": self.upload(path), "label": label})
        out = self.call(
            "POST",
            "/cast",
            {"kind": kind, "name": name, "description": description, "images": pics},
        )
        return out.get("card") or out

    # ---- 电影 ----
    def plan_film(self, screenplay: dict, takes: int = 1, ref: str | None = None) -> dict:
        body = {"screenplay": screenplay, "takes_per_shot": takes, "director": "agent"}
        if ref:
            body["ref"] = ref
        return self.call("POST", "/drama/plan", body)

    def start_film(self, screenplay: dict, takes: int = 1, ref: str | None = None) -> dict:
        body = {"screenplay": screenplay, "takes_per_shot": takes, "director": "agent"}
        if ref:
            body["ref"] = ref
        out = self.call("POST", "/drama", body)
        return out.get("drama") or out.get("film") or out

    def film(self, film_id: str, since: int | None = None, wait: int = 0) -> dict:
        params = {}
        if since is not None:
            params["since"] = since
        if wait:
            params["wait"] = wait
        out = self.call("GET", f"/drama/{film_id}", **params)
        return out.get("drama") or out.get("film") or out

    def sheet(self, film_id: str, shot: str) -> dict:
        return self.call("GET", f"/drama/{film_id}/shots/{shot}/sheet")

    def choose(self, film_id: str, shot: str, take: Any, revision: int | None = None) -> dict:
        body = {"take": take}
        if revision is not None:
            body["revision"] = revision
        return self.call("POST", f"/drama/{film_id}/shots/{shot}/choose", body)

    def reshoot(
        self, film_id: str, shot: str, note: str | None = None, takes: int | None = None
    ) -> dict:
        body: dict = {}
        if note:
            body["note"] = note
        if takes:
            body["takes"] = takes
        return self.call("POST", f"/drama/{film_id}/shots/{shot}/reshoot", body)

    def finish(self, film_id: str) -> dict:
        return self.call("POST", f"/drama/{film_id}/finish", {})

    def cancel_film(self, film_id: str) -> dict:
        return self.call("POST", f"/drama/{film_id}/cancel", {})

    def wait_job(self, job_id: str, timeout_s: float = 3600) -> dict:
        end = time.monotonic() + timeout_s
        while True:
            job = self.call("GET", f"/jobs/{job_id}", wait=25).get("job")
            if (
                job["status"] in ("succeeded", "failed", "cancelled", "interrupted")
                or time.monotonic() > end
            ):
                return job
