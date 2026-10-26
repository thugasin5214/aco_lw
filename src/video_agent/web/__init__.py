"""Web 控制台：进度（DAG / 流水线 / 产物）、审阅操作、项目与对话历史。

审阅与对话走 Actions（与 Telegram 相同的函数）；需要 LLM 的批注进规划器队列，结果写入对话历史。
由 `va daemon` 启动时与 Worker 同进程；`va serve` 单独启动时没有 Worker，排队任务不会执行。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .. import llm
from ..config import ROOT, Settings
from ..memory import snapshot
from ..planner import MODES
from ..store import PROJECT_ZH, ROUND_ZH, STATE_ZH
from ..studio import StudioError

STATIC = Path(__file__).parent / "index.html"
TASK_KEYS = (
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
    "project_id",
    "round_id",
    "idx",
)


def create_app(cfg: Settings, actions=None) -> FastAPI:
    if actions is None:  # 独立运行：自带规划器线程，但没有 Worker
        from ..daemon import Actions
        from ..planner import Planner
        from ..scheduler import Orchestrator
        from ..studio import Studio

        orch = Orchestrator(cfg)
        actions = Actions(Studio(orch, cfg.round_size), Planner(cfg, orch.store, orch.registry))
        threading.Thread(
            target=actions.planner_loop, args=(threading.Event(),), daemon=True
        ).start()
    studio = actions.studio
    store = studio.store
    registry = studio.orch.registry
    app = FastAPI(title="aco_lw 控制台", docs_url=None, redoc_url=None)

    def slim(t: dict) -> dict:
        return {k: t.get(k) for k in TASK_KEYS}

    def collect():
        out: list[dict] = []
        return out, lambda text, buttons=None: out.append({"text": text, "buttons": buttons})

    def guard(fn):
        try:
            return fn()
        except (StudioError, KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    # ---- 页面 ----
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC.read_text()

    @app.get("/progress", response_class=HTMLResponse)
    def progress() -> str:
        return (ROOT / "docs" / "progress.html").read_text()

    # ---- 状态 ----
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
        d = store.get_kv("daemon") or {}
        return {
            "memory": snapshot(),
            "comfy": comfy,
            "llm": {
                "model": cfg.llm_model,
                "loaded": m is not None,
                "ttl_ms": m and m.get("ttlMs"),
                "mode": actions.planner.mode,
                "mode_zh": MODES[actions.planner.mode],
            },
            "daemon": {
                "alive": bool(d) and time.time() - d.get("ts", 0) < 15,
                "paused": bool(store.get_kv("paused")),
                "busy": d.get("busy"),
            },
            "queued": len(store.by_state("draft")),
            "project": studio.current_project(),
            "pending_review": len(store.rounds(state="review"))
            + sum(1 for t in store.by_state("review") if not t["round_id"]),
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "states": STATE_ZH,
            "round_states": ROUND_ZH,
            "project_states": PROJECT_ZH,
            "templates": {
                n: {"title": t.title, "kind": t.kind, "model": t.model}
                for n, t in registry.templates.items()
            },
        }

    # ---- 任务 ----
    @app.get("/api/tasks")
    def tasks(project: str | None = None) -> list[dict]:
        return [slim(t) for t in store.list(500, project)]

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str) -> dict:
        t = store.get(task_id)
        if t is None:
            raise HTTPException(404)
        t["events"] = store.events(task_id)
        return t

    # ---- DAG：轮为节点（含候选缩略图），独立任务也是节点 ----
    @app.get("/api/graph")
    def graph(project: str | None = None) -> list[dict]:
        rounds = store.rounds(project)
        ts = store.list(1000, project)
        node_of = {t["id"]: (t["round_id"] or t["id"]) for t in ts}
        nodes = []
        for r in rounds:
            cands = store.round_tasks(r["id"])
            parents = [r["parent_round"]] if r["parent_round"] else []
            parents += [node_of.get(c["parent_id"]) for c in cands if c["parent_id"]]
            nodes.append(
                {
                    "id": r["id"],
                    "type": "round",
                    "title": r["title"] or r["template"],
                    "state": r["state"],
                    "created": r["created"],
                    "updated": r["updated"],
                    "template": r["template"],
                    "tier": r["tier"],
                    "decision": r["decision"],
                    "parents": sorted({p for p in parents if p}),
                    "tasks": [slim(c) for c in cands],
                }
            )
        for t in ts:
            if t["round_id"]:
                continue
            nodes.append(
                {
                    "id": t["id"],
                    "type": "task",
                    "title": t["title"] or t["template"],
                    "state": t["state"],
                    "created": t["created"],
                    "updated": t["updated"],
                    "template": t["template"],
                    "tier": t["tier"],
                    "parents": [node_of[t["parent_id"]]] if t["parent_id"] in node_of else [],
                    "tasks": [slim(t)],
                }
            )
        return nodes

    # ---- 项目与历史 ----
    @app.get("/api/projects")
    def projects() -> list[dict]:
        out = []
        for p in store.list_projects():
            ts = store.list(1000, p["id"])
            pack = store.pack(p["id"])
            cover = next((t for t in ts for o in t["outputs"] if o["kind"] == "image"), None)
            out.append(
                {
                    **p,
                    "tasks": len(ts),
                    "rounds": len(store.rounds(p["id"])),
                    "pack_version": pack and pack["version"],
                    "cover": cover
                    and {"task_id": cover["id"], "file": cover["outputs"][0]["file"]},
                }
            )
        return out

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(404)
        return {
            **p,
            "packs": store.pack_history(project_id),
            "messages": store.messages(project_id),
            "rounds": [
                {**r, "tasks": [slim(t) for t in store.round_tasks(r["id"])]}
                for r in store.rounds(project_id)
            ],
        }

    @app.post("/api/projects")
    def new_project(name: str = Body(..., embed=True)) -> dict:
        return guard(lambda: studio.new_project(name))

    @app.post("/api/projects/{project_id}/use")
    def use_project(project_id: str) -> dict:
        return guard(lambda: studio.use_project(project_id))

    @app.post("/api/projects/{project_id}/done")
    def finish_project(project_id: str) -> dict:
        return guard(lambda: studio.finish_project(project_id))

    # ---- 审阅 ----
    @app.get("/api/review")
    def review_queue() -> list[dict]:
        items = [
            {"type": "round", **r, "tasks": [slim(t) for t in store.round_tasks(r["id"])]}
            for r in store.rounds(state="review")
        ]
        items += [
            {
                "type": "task",
                "id": t["id"],
                "title": t["title"] or t["template"],
                "template": t["template"],
                "tier": t["tier"],
                "created": t["created"],
                "project_id": t["project_id"],
                "tasks": [slim(t)],
            }
            for t in store.by_state("review")
            if not t["round_id"]
        ]
        return sorted(items, key=lambda x: x["created"])

    @app.post("/api/rounds/{round_id}/select")
    def select(round_id: str, idx: int = Body(...), role: str | None = Body(None)) -> dict:
        rnd = guard(lambda: studio.select(round_id, idx, origin="web"))
        if role:
            guard(lambda: studio.lock(rnd["decision"]["task_id"], role))
        return rnd

    @app.post("/api/rounds/{round_id}/redo")
    def redo(round_id: str) -> dict:
        return guard(lambda: studio.redo(round_id, origin="web"))

    @app.post("/api/rounds/{round_id}/refine")
    def refine(round_id: str, idx: int = Body(...), note: str = Body(...)) -> dict:
        out, reply = collect()
        guard(lambda: actions.refine_note(round_id, idx, note, "web", actions._say("web", reply)))
        return {"messages": out}

    @app.post("/api/rounds/{round_id}/reject")
    def reject(round_id: str, note: str = Body("", embed=True)) -> dict:
        out, reply = collect()
        if note.strip():
            guard(lambda: actions.reject_note(round_id, note, "web", actions._say("web", reply)))
        else:
            guard(lambda: studio.reject(round_id, None, origin="web"))
        return {"messages": out}

    @app.post("/api/tasks/{task_id}/approve")
    def approve(
        task_id: str,
        accept: bool = Body(True),
        role: str | None = Body(None),
        note: str | None = Body(None),
    ) -> dict:
        r = guard(lambda: studio.approve_task(task_id, accept, note))
        if accept and role:
            guard(lambda: studio.lock(task_id, role))
        return r

    @app.post("/api/tasks/{task_id}/redo")
    def redo_task(task_id: str) -> dict:
        return guard(lambda: studio.redo_task(task_id, origin="web"))

    # ---- 控制台对话（与 Telegram 同一套命令 / 自由文本） ----
    @app.post("/api/chat")
    def chat(text: str = Body(..., embed=True)) -> dict:
        out, reply = collect()
        guard(lambda: actions.inbound("web", text.strip(), reply))
        return {"messages": out}

    @app.get("/api/messages")
    def messages(project: str | None = None, after: int = 0) -> list[dict]:
        pid = project or studio.current_project()["id"]
        return [m for m in store.messages(pid) if m["id"] > after]

    # ---- 文件 ----
    @app.get("/files/{task_id}/{name}")
    def file(task_id: str, name: str) -> FileResponse:
        return _safe_file(cfg.jobs_dir, cfg.jobs_dir / task_id / name)

    @app.get("/packfile/{project_id}/{version}/{name}")
    def packfile(project_id: str, version: str, name: str) -> FileResponse:
        return _safe_file(store.packs_dir, store.packs_dir / project_id / version / name)

    return app


def _safe_file(base: Path, path: Path) -> FileResponse:
    base, path = base.resolve(), path.resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
