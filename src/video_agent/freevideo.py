"""FreeVideo agent API client (/freevideo/agent/v1 on the local ComfyUI).

Used for video consistency: character cards (up to 6 labelled images each + optional voice) and multi-shot films (several takes per shot,
the next shot continues from the take picked in the previous one).

- The token is read in-process only from FreeVideo-local/agent/token; never printed, written to other files or logged.
- Only does what the API allows external agents to do; pausing / resuming a film is only possible for the user in the FreeVideo page.
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
        self.status = status
        err = body.get("error", {}) if isinstance(body, dict) else {}
        err = err if isinstance(err, dict) else {"code": str(err)}
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

    # ---- transport ----
    def _headers(self) -> dict:
        # read every time: the user may reset the token
        token = (self.root / "agent" / "token").read_text().strip()
        return {"Authorization": "Bearer " + token}

    def call(self, method: str, path: str, body: Any = None, **params) -> Any:
        if not self._hello and path != "/connect/hello":
            self._hello = True
            try:
                self.call("POST", "/connect/hello", {"client": self.client_name, "via": "http"})
            except FreeVideoError:
                # the hello only lets the user see in the FreeVideo page who is connected; failing
                # it does not affect calls
                pass
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
        """Download a result file (url may be a path relative to the ComfyUI root)."""
        full = url if url.startswith("http") else self.base.split("/freevideo/")[0] + url
        with self.http.stream("GET", full, headers=self._headers()) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    # ---- status ----
    def status(self) -> dict:
        return self.call("GET", "")

    # ---- media ----
    def upload(self, path: str | Path) -> str:
        """Upload an image / video / audio; returns the media name inside FreeVideo."""
        p = Path(path)
        out = self.call(
            "POST",
            "/media",
            {"name": p.name, "data_base64": base64.b64encode(p.read_bytes()).decode()},
        )
        media = out.get("media") or out
        return media.get("file") or media.get("name") or media["id"]

    # ---- character cards ----
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
        """images: [(local path, label)], label one of front / three_quarter / side / back / face / sheet / other."""
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

    # ---- films ----
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
