"""只读 Web 进度台：系统状态、任务 DAG、阶段流水线、产物预览、项目进度页。

只读数据库与任务目录；唯一会访问 ComfyUI 的是 GET /queue（状态展示），不做任何副作用。
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .. import llm
from ..config import ROOT, Settings
from ..memory import snapshot
from ..registry import Registry
from ..store import STATE_ZH, Store

STATIC = Path(__file__).parent / "index.html"


def create_app(cfg: Settings) -> FastAPI:
    app = FastAPI(title="aco_lw 进度台", docs_url=None, redoc_url=None)
    store = Store(cfg.db_path, cfg.jobs_dir)
    registry = Registry(cfg.templates_dir)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC.read_text()

    @app.get("/progress", response_class=HTMLResponse)
    def progress() -> str:
        return (ROOT / "docs" / "progress.html").read_text()

    @app.get("/api/system")
    def system() -> dict:
        comfy = {"online": False}
        try:
            q = httpx.get(f"{cfg.comfy_url}/queue", timeout=2).json()
            comfy = {
                "online": True,
                "running": len(q["queue_running"]),
                "pending": len(q["queue_pending"]),
            }
        except (httpx.HTTPError, ValueError):
            pass
        m = llm.loaded(cfg.llm_model)
        return {
            "memory": snapshot(),
            "comfy": comfy,
            "llm": {
                "model": cfg.llm_model,
                "loaded": m is not None,
                "status": m and m.get("status"),
                "ttl_ms": m and m.get("ttlMs"),
            },
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "states": STATE_ZH,
            "templates": {
                n: {"title": t.title, "kind": t.kind, "model": t.model}
                for n, t in registry.templates.items()
            },
        }

    @app.get("/api/tasks")
    def tasks() -> list[dict]:
        return [
            {
                k: t[k]
                for k in (
                    "id",
                    "created",
                    "updated",
                    "state",
                    "template",
                    "kind",
                    "tier",
                    "title",
                    "parent_id",
                    "outputs",
                    "error",
                )
            }
            for t in store.list(500)
        ]

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str) -> dict:
        t = store.get(task_id)
        if t is None:
            raise HTTPException(404)
        t["events"] = store.events(task_id)
        return t

    @app.get("/files/{task_id}/{name}")
    def file(task_id: str, name: str) -> FileResponse:
        base = cfg.jobs_dir.resolve()
        path = (cfg.jobs_dir / task_id / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    return app
