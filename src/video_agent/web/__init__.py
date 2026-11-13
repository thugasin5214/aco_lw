"""Web console: progress (DAG / pipeline / outputs), review actions, projects and chat history.

Review and chat go through Actions (the same functions as Telegram); notes that need the LLM go to the planner queue and results land in chat history.
Started by `va daemon` it shares the process with the Worker; started alone by `va serve` there is no Worker and queued tasks do not run.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import jsonschema
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from .. import config, llm
from ..config import ROOT, Settings
from ..freevideo import FreeVideoError
from ..memory import snapshot
from ..planner import MODES
from ..production import ProductionError
from ..render import TIER_KEYS
from ..store import PROJECT_ZH, ROUND_ZH, STATE_ZH, TransitionError
from ..studio import StudioError
from ..variants import Workshop

STATIC = Path(__file__).parent / "index.html"
RUNBOOK_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>aco_lw Runbook</title>
<style>
:root{--bg:#f6f8fb;--card:#fff;--line:#c9d3e0;--text:#152030;--muted:#526175;--accent:#1f5f99;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#10151e;--card:#1a2331;
--line:#344258;--text:#e7edf7;--muted:#a9b8cc;--accent:#8ecbff;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#10151e;--card:#1a2331;--line:#344258;--text:#e7edf7;--muted:#a9b8cc;
--accent:#8ecbff;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15.5px/1.75 system-ui,-apple-system,"PingFang SC",sans-serif}
main{max-width:980px;margin:auto;padding:28px 20px 70px}h1{font-size:28px;margin:4px 0 8px}
h2{font-size:20px;margin:36px 0 10px;padding-top:8px;border-top:1px solid var(--line)}h3{font-size:16.5px;margin:22px 0 6px}
a{color:var(--accent)}hr{border:0}code{color:var(--accent);font-size:.92em;overflow-wrap:anywhere}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:auto}
pre code{color:var(--text)}table{width:100%;border-collapse:collapse;font-size:14.5px;display:block;overflow-x:auto}
th,td{padding:7px 10px;border:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--card)}
</style></head><body><main>{{body}}</main></body></html>"""
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
    if actions is None:  # standalone: its own planner thread, but no Worker
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
        except (
            StudioError,
            KeyError,
            ValueError,
            TransitionError,
            ProductionError,
            FreeVideoError,
        ) as e:
            raise HTTPException(400, str(e)) from e
        except jsonschema.ValidationError as e:
            raise HTTPException(400, f"schema: {e.message}") from e

    # ---- pages ----
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return STATIC.read_text()

    def _doc(name: str, ext: str, lang: str | None) -> Path:
        """English is the main file; with lang=zh prefer *.zh-CN.<ext>."""
        zh = ROOT / "docs" / f"{name}.zh-CN.{ext}"
        if (lang or cfg.language).startswith("zh") and zh.exists():
            return zh
        return ROOT / "docs" / f"{name}.{ext}"

    @app.get("/progress", response_class=HTMLResponse)
    def progress(lang: str | None = None) -> str:
        return _doc("progress", "html", lang).read_text()

    @app.get("/architecture", response_class=HTMLResponse)
    def architecture(lang: str | None = None) -> str:
        return _doc("architecture", "html", lang).read_text()

    @app.get("/architecture.html", response_class=HTMLResponse)
    def architecture_en() -> str:
        return _doc("architecture", "html", "en").read_text()

    @app.get("/architecture.zh-CN.html", response_class=HTMLResponse)
    def architecture_zh() -> str:
        return _doc("architecture", "html", "zh").read_text()

    @app.get("/runbook", response_class=HTMLResponse)
    def runbook(lang: str | None = None) -> str:
        import markdown

        body = markdown.markdown(
            _doc("runbook", "md", lang).read_text(), extensions=["tables", "fenced_code"]
        )
        return RUNBOOK_PAGE.replace("{{body}}", body).replace(
            'lang="zh-CN"', 'lang="zh-CN"' if (lang or "").startswith("zh") else 'lang="en"'
        )

    # ---- status ----
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
            + sum(1 for t in store.by_state("review") if not t["round_id"])
            + len(_stage_reviews()),
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "states": STATE_ZH,
            "round_states": ROUND_ZH,
            "project_states": PROJECT_ZH,
            "templates": {
                n: {"title": t.title, "kind": t.kind, "model": t.model, "origin": t.origin}
                for n, t in registry.templates.items()
            },
        }

    # ---- tasks ----
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

    # ---- DAG: rounds are nodes (with candidate thumbnails); standalone tasks are nodes too ----
    def _production_graph(project: str | None) -> tuple[dict[str, list[str]], list[dict]]:
        engine = getattr(actions, "production", None)
        extra: dict[str, list[str]] = {}
        nodes: list[dict] = []
        if engine is None:
            return extra, nodes
        for prod in store.productions(project):
            wf = engine.workflows.get(prod["workflow"])
            titles = {st.id: st.title or st.id for st in wf.stages} if wf else {}
            arts = store.artifacts(prod["id"])
            by_ver = {(a["stage"], a["key"], a["version"]): a for a in arts}
            latest: dict[tuple[str, str], dict] = {}
            for a in arts:
                if not a["round_id"] and a["status"] != "superseded":
                    latest[(a["stage"], a["key"])] = a

            def node_id(a: dict, pid: str = prod["id"]) -> str:
                return a["round_id"] or f"stage:{pid}:{a['stage']}:{a['key']}"

            def parents(a: dict, by_ver=by_ver, latest=latest) -> list[str]:
                out = []
                for d in a["deps"]:
                    dep = by_ver.get((d["stage"], d["key"], d.get("version")))
                    # dependency recorded by item fingerprint: link to that stage's node
                    if dep is None:
                        dep = latest.get((d["stage"], d["key"]))
                    if dep is not None:
                        out.append(node_id(dep))
                return out

            for a in arts:
                if a["round_id"]:
                    extra.setdefault(a["round_id"], []).extend(parents(a))
            for (stage, key), a in latest.items():
                label = titles.get(stage, stage) + ("" if key == "main" else f" · {key}")
                nodes.append(
                    {
                        "id": node_id(a),
                        "type": "stage",
                        "title": f"{prod['title']} · {label}",
                        "state": a["status"],
                        "created": a["created"],
                        "updated": a["updated"],
                        "template": "",
                        "tier": f"v{a['version']}",
                        "parents": sorted(set(parents(a))),
                        "tasks": [],
                        "production_id": prod["id"],
                        "stage": stage,
                        "data": a["data"],
                        "note": a["note"],
                    }
                )
        return extra, nodes

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
        # productions: planning stages are nodes too; render rounds link upstream by artifact
        # dependency (bible → casting → storyboard → keyframes → video → edit → final)
        extra, stage_nodes = _production_graph(project)
        for n in nodes:
            n["parents"] = sorted(set(n["parents"]) | set(extra.get(n["id"], [])))
            art = store.artifact_by_round(n["id"])
            # a production's render round: attach its stage and item (the console can rerun it by
            # hand)
            if art:
                n.update(production_id=art["production_id"], stage=art["stage"], key=art["key"])
        nodes += stage_nodes
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

    # ---- projects and history ----
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
        cur = studio.current_session(project_id) if p["state"] == "active" else None
        return {
            **p,
            "packs": store.pack_history(project_id),
            "sessions": store.sessions(project_id),
            "current_session": cur and cur["id"],
            "memories": store.memories(project_id),
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

    # ---- review ----
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

    # ---- delete outputs (with data/jobs and the originals this system generated in ComfyUI) ----
    @app.post("/api/rounds/{round_id}/delete")
    def delete_round(round_id: str) -> dict:
        return guard(lambda: studio.delete_round(round_id))

    @app.post("/api/tasks/{task_id}/delete")
    def delete_task(task_id: str) -> dict:
        return guard(lambda: studio.delete_task(task_id))

    @app.post("/api/projects/{project_id}/purge")
    def purge_project(project_id: str, scope: str = Body("review", embed=True)) -> dict:
        return guard(lambda: studio.purge_project(project_id, scope))

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

    @app.post("/api/rounds/{round_id}/reopen")
    def reopen_round(round_id: str) -> dict:
        out, reply = collect()
        guard(lambda: actions.reopen(round_id, "web", actions._say("web", reply)))
        return {"messages": out}

    @app.post("/api/tasks/{task_id}/reopen")
    def reopen_task(task_id: str) -> dict:
        t = store.get(task_id)
        if t and t["round_id"]:
            return reopen_round(t["round_id"])
        return guard(lambda: studio.reopen_task(task_id, origin="web"))

    @app.post("/api/tasks/{task_id}/redo")
    def redo_task(task_id: str) -> dict:
        return guard(lambda: studio.redo_task(task_id, origin="web"))

    # ---- console chat (same commands / free text as Telegram) ----
    @app.post("/api/chat")
    # FastAPI body declaration
    def chat(text: str = Body(...), attachments: list[str] = Body(default_factory=list)) -> dict:  # noqa: B008
        out, reply = collect()
        atts = []
        for uid in attachments:
            up = store.get_upload(uid)
            if up:
                atts.append({"id": up["id"], "name": up["name"]})
        guard(lambda: actions.inbound("web", text.strip(), reply, atts or None))
        return {"messages": out}

    @app.post("/api/action")
    def action(data: str = Body(..., embed=True)) -> dict:
        """Buttons in the console chat (same format as Telegram callback_data)."""
        out, reply = collect()
        guard(lambda: actions.button("web", data, reply))
        return {"messages": out}

    # ---- sessions ----
    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str, after: int = 0) -> list[dict]:
        return store.session_messages(session_id, after)

    @app.post("/api/sessions")
    def new_session(title: str | None = Body(None, embed=True)) -> dict:
        return guard(lambda: studio.new_session(title))

    @app.post("/api/sessions/{session_id}/use")
    def use_session(session_id: str) -> dict:
        s = store.get_session(session_id)
        if s is None:
            raise HTTPException(404)
        guard(lambda: studio.use_project(s["project_id"]))
        return guard(lambda: studio.use_session(session_id, s["project_id"]))

    @app.post("/api/sessions/{session_id}/delete")
    def delete_session(session_id: str) -> dict:
        if store.get_session(session_id) is None:
            raise HTTPException(404, f"No session {session_id}.")
        return guard(lambda: studio.delete_session(session_id))

    @app.post("/api/sessions/{session_id}/rename")
    def rename_session(session_id: str, title: str = Body(..., embed=True)) -> dict:
        return store.update_session(session_id, title=title.strip())

    @app.get("/api/sessions/{session_id}/context")
    def session_context(session_id: str) -> dict:
        """The full context the next LLM call will send (the last message is a preview placeholder)."""
        s = store.get_session(session_id)
        if s is None:
            raise HTTPException(404)
        msgs = actions.planner.build("（下一条消息）", s["project_id"], session_id, None)
        chars = sum(len(m["content"]) for m in msgs)
        return {
            "messages": msgs,
            "chars": chars,
            "summary_upto": s["summary_upto"],
            "rules_file": "prompts/planner_rules.md",
        }

    # ---- project memory ----
    @app.post("/api/memories")
    def add_memory(text: str = Body(..., embed=True)) -> dict:
        return guard(lambda: studio.add_memory(text))

    @app.post("/api/memories/{memory_id}/{op}")
    def memory_op(memory_id: int, op: str, text: str | None = Body(None, embed=True)) -> dict:
        return guard(lambda: studio.set_memory(memory_id, op, text))

    # ---- upload reference images (request body is the file itself, no extra dependency) ----
    @app.post("/api/upload")
    async def upload(request: Request, name: str) -> dict:
        data = await request.body()
        if len(data) > 50 * 2**20:
            raise HTTPException(413, "文件超过 50 MB")
        up = guard(lambda: studio.save_upload(name, data))
        return {"id": up["id"], "name": up["name"], "kind": up["kind"]}

    @app.get("/uploads/{upload_id}")
    def upload_file(upload_id: str) -> FileResponse:
        up = store.get_upload(upload_id)
        if up is None:
            raise HTTPException(404)
        return _safe_file(store.jobs_dir.parent / "uploads", Path(up["path"]))

    # ---- manual tweaks (W1) ----
    @app.get("/api/templates/{name}")
    def template_info(name: str) -> dict:
        t = guard(lambda: registry.template(name))
        return {
            "name": t.name,
            "title": t.title,
            "kind": t.kind,
            "slots": {k: v.model_dump() for k, v in t.slots.items() if k not in TIER_KEYS},
            "tiers": {k: {**v.values(), "verified": v.verified} for k, v in t.tiers.items()},
        }

    @app.post("/api/tasks/{task_id}/tweak")
    def tweak(
        task_id: str,
        params: dict = Body(...),  # noqa: B008 — FastAPI body declaration
        tier: str | None = Body(None),
        n: int = Body(1),
    ) -> dict:
        return guard(lambda: studio.tweak(task_id, params, tier, n))

    # ---- scheduling and agent runs (U1 / A3) ----
    @app.get("/api/queue")
    def queue_view() -> dict:
        rm = studio.orch.resources
        drafts = store.by_state("draft")
        titles = {t["id"]: t["title"] or t["template"] for t in drafts}
        active = rm.render_active
        if active:
            t = store.get(active["task_id"])
            active = {
                **active,
                "title": t and (t["title"] or t["template"]),
                "state": t and t["state"],
            }
        return {
            "render_active": active,
            "resident_family": rm.resident_family,
            "llm": {"loaded": rm.llm.loaded(), "in_flight": rm.llm.in_flight},
            "plan": [{**p, "title": titles.get(p["task_id"])} for p in rm.plan(drafts)],
            "runs": [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "status",
                        "waiting",
                        "origin_text",
                        "project_id",
                        "session_id",
                        "steps",
                        "updated",
                    )
                }
                for r in store.runs(
                    None, "running", "waiting_resources", "waiting_event", "waiting_user"
                )
            ],
            "history": list(rm.history)[-360:],
        }

    # ---- for the local agent CLIs' MCP server (only accepts runs that were delegated out) ----
    @app.get("/api/agent/tools")
    def agent_tools() -> list[dict]:
        return [t["function"] for t in actions.runtime.spec]

    @app.post("/api/agent/tool")
    def agent_tool(
        run_id: str = Body(...),
        name: str = Body(...),
        arguments: dict = Body(default_factory=dict),  # noqa: B008 — FastAPI body declaration
    ) -> dict:
        return actions.runtime.external_tool(
            run_id, name, arguments, timeout=cfg.agent_cli_timeout_s
        )

    @app.get("/api/runs")
    def runs(session: str) -> list[dict]:
        return [
            {
                k: r[k]
                for k in ("id", "status", "waiting", "origin_text", "steps", "created", "updated")
            }
            for r in store.runs(session)
        ][-20:]

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict:
        actions.runtime.cancel(run_id)
        return {"ok": True}

    # ---- production pipeline (stage 3) ----
    def _stage_reviews() -> list[dict]:
        prod = getattr(actions, "production", None)
        return prod.pending_reviews() if prod else []

    @app.get("/api/review/stages")
    def review_stages() -> list[dict]:
        return _stage_reviews()

    def _production():
        prod = getattr(actions, "production", None)
        if prod is None:
            raise HTTPException(503, "production engine runs inside the daemon (va daemon)")
        return prod

    @app.get("/api/workflows")
    def workflows() -> list[dict]:
        return [
            {
                "name": w.name,
                "title": w.title,
                "description": w.description,
                "settings": w.settings,
                "stages": [
                    {
                        "id": s.id,
                        "title": s.title or s.id,
                        "kind": s.kind,
                        "needs": s.needs,
                        "skill": s.skill,
                        "gate": s.gate,
                        "confirm": s.confirm,
                    }
                    for s in w.stages
                ],
            }
            for w in _production().workflows.values()
        ]

    @app.get("/api/skills")
    def skills() -> list[dict]:
        return _production().skills.list()

    @app.get("/api/productions")
    def productions(project: str | None = None) -> list[dict]:
        return store.productions(project)

    @app.post("/api/productions")
    def create_production(
        workflow: str = Body(...),
        title: str = Body(...),
        inputs: dict = Body(...),  # noqa: B008 — e.g. {"source": "novel text"}
        settings: dict = Body(default_factory=dict),  # noqa: B008
        project_id: str | None = Body(None),
    ) -> dict:
        return guard(lambda: _production().create(workflow, title, inputs, settings, project_id))

    @app.get("/api/productions/{pid}/plan")
    def production_plan(pid: str) -> dict:
        return guard(lambda: _production().plan(pid))

    @app.get("/api/productions/{pid}/artifacts")
    def production_artifacts(pid: str, stage: str | None = None) -> list[dict]:
        return store.artifacts(pid, stage)

    @app.post("/api/productions/{pid}/run")
    def production_run(
        pid: str,
        stage: str = Body(...),
        keys: list[str] | None = Body(None),  # noqa: B008
        note: str | None = Body(None),
        overrides: dict | None = Body(None),  # noqa: B008
    ) -> list[dict]:
        return guard(
            lambda: _production().run_stage(
                pid, stage, keys, note, origin="web", overrides=overrides
            )
        )

    @app.post("/api/productions/{pid}/provide")
    def production_provide(
        pid: str,
        stage: str = Body(...),
        key: str = Body(...),
        upload_id: str = Body(...),
        note: str | None = Body(None),
    ) -> dict:
        up = store.get_upload(upload_id)
        if up is None:
            raise HTTPException(404, "upload not found")
        return guard(lambda: _production().provide(pid, stage, key, up["path"], note))

    @app.get("/api/artifacts/{aid}/file/{i}")
    def artifact_file(aid: str, i: int) -> FileResponse:
        """Files of production artifacts that belong to no task (images you provided, reference images in the material pack)."""
        art = store.get_artifact(aid)
        if art is None:
            raise HTTPException(404)
        data = art["data"] or {}
        files = [o["path"] for o in data.get("outputs") or []] + [
            r["path"] for r in data.get("references") or []
        ]
        if not 0 <= i < len(files):
            raise HTTPException(404)
        path = Path(files[i])
        base = store.jobs_dir.parent  # data/: uploads and production directories live here
        if base.resolve() not in path.resolve().parents:
            raise HTTPException(404)
        return _safe_file(base, path)

    @app.post("/api/productions/{pid}/pause")
    def production_pause(pid: str, stop_running: bool = Body(False, embed=True)) -> dict:
        return guard(lambda: _production().pause(pid, stop_running))

    @app.post("/api/productions/{pid}/resume")
    def production_resume(pid: str) -> dict:
        return guard(lambda: _production().resume(pid))

    @app.post("/api/pause")
    def pause_all(stop_running: bool = Body(False, embed=True)) -> dict:
        """Global pause: the Worker takes no new tasks (the queue stays); with stop_running the running one is interrupted."""
        store.set_kv("paused", True)
        cur = actions.worker.stop_current() if stop_running and actions and actions.worker else None
        return {"paused": True, "interrupted": cur and cur["id"]}

    @app.post("/api/resume")
    def resume_all() -> dict:
        store.set_kv("paused", False)
        return {"paused": False}

    @app.post("/api/productions/{pid}/settings")
    def production_settings(pid: str, settings: dict = Body(..., embed=True)) -> dict:  # noqa: B008
        return guard(lambda: _production().update_settings(pid, settings))

    @app.post("/api/productions/{pid}/review")
    def production_review(
        pid: str,
        stage: str = Body(...),
        action: str = Body(...),
        note: str | None = Body(None),
        data: dict | None = Body(None),  # noqa: B008
        key: str = Body("main"),
    ) -> dict:
        return guard(lambda: _production().review(pid, stage, action, note, data, key, "web"))

    @app.get("/api/settings")
    def settings() -> dict:
        return {"language": cfg.language, "comfy_url": cfg.comfy_url}

    # ---- settings window: editable config.toml keys + runtime switches + local LLM loading ----
    llm_job: dict = {"state": "idle", "error": None}

    def _runtime_cfg() -> dict:
        rt = actions.runtime if hasattr(actions, "runtime") else None
        return {
            "llm_mode": actions.planner.mode,
            "agent_brain": rt.brain() if rt else "local",
            "permission_mode": rt.permission_mode() if rt else cfg.permission_mode,
        }

    @app.get("/api/config")
    def get_config() -> dict:
        from ..planner import BRAINS

        rows = []
        for group, name, desc, apply in config.EDITABLE:
            v = getattr(cfg, name)
            row = {
                "group": group,
                "name": name,
                "desc": desc,
                "apply": apply,
                "type": type(v).__name__,
                "choices": config.CHOICES.get(name),
            }
            if name in config.SECRET:
                row["secret"], row["is_set"] = True, bool(v)
            else:
                row["value"] = ", ".join(map(str, v)) if isinstance(v, list) else v
            rows.append(row)
        loaded = llm.loaded_all()
        return {
            "fields": rows,
            "runtime": _runtime_cfg(),
            "choices": {
                "llm_mode": list(MODES),
                "agent_brain": list(BRAINS),
                "permission_mode": ["default", "bypass"],
            },
            "models": llm.models(),
            "loaded": [
                {
                    "id": m.get("identifier"),
                    "context": m.get("contextLength"),
                    "ttl_ms": m.get("ttlMs"),
                    "status": m.get("status"),
                }
                for m in loaded
            ],
            "llm_job": llm_job,
        }

    @app.post("/api/config")
    def set_config(
        values: dict = Body({}),  # noqa: B008
        runtime: dict = Body({}),  # noqa: B008
    ) -> dict:
        from .. import i18n
        from ..planner import BRAINS

        clean = {}
        for name, v in values.items():
            if name in config.SECRET and (v is None or v == ""):
                continue  # empty secret = leave unchanged
            clean[name] = guard(lambda n=name, v=v: config.coerce(n, v))
        restart = [n for n in clean if {e[1]: e[3] for e in config.EDITABLE}[n] == "restart"]
        if clean:
            if cfg.config_path:
                guard(lambda: config.save(clean, cfg.config_path))
            for n, v in clean.items():
                setattr(cfg, n, v)
            if "language" in clean:
                i18n.set_language(cfg.language)
            if "agent_max_steps" in clean and hasattr(actions, "runtime"):
                actions.runtime.max_steps = cfg.agent_max_steps
        if runtime.get("agent_brain"):
            if runtime["agent_brain"] not in BRAINS:
                raise HTTPException(400, f"unknown brain {runtime['agent_brain']}")
            store.set_kv("agent_brain", runtime["agent_brain"])
        if runtime.get("permission_mode"):
            if runtime["permission_mode"] not in ("default", "bypass"):
                raise HTTPException(400, "permission_mode: default | bypass")
            store.set_kv("permission_mode", runtime["permission_mode"])
        if runtime.get("llm_mode") and runtime["llm_mode"] != actions.planner.mode:
            mode = runtime["llm_mode"]
            if mode not in MODES:
                raise HTTPException(400, f"unknown mode {mode}")
            store.set_kv("llm_mode", mode)  # resident modes load the LLM: do it in the background
            threading.Thread(
                target=_llm_bg, args=(lambda: actions.planner.set_mode(mode),), daemon=True
            ).start()
        return {"saved": sorted(clean), "restart": restart, "runtime": _runtime_cfg()}

    def _llm_bg(fn) -> None:
        llm_job.update(state="working", error=None)
        try:
            fn()
            llm_job.update(state="idle")
        except Exception as e:  # noqa: BLE001 — the result shows in the settings window
            llm_job.update(state="error", error=str(e))

    @app.post("/api/llm/load")
    def llm_load(reload: bool = Body(False, embed=True)) -> dict:
        """Load with the current settings (reload: unload first, then load with the new options). Refused during inference / an exclusive render."""
        rm = studio.orch.resources
        if llm_job["state"] == "working":
            raise HTTPException(409, "上一次加载 / 卸载还没结束")
        ok, why = rm.llm_admit()
        if not ok:
            raise HTTPException(409, f"现在不能加载：{why}")
        is_loaded = rm.llm.loaded()
        if is_loaded and not reload:
            return {"state": "loaded"}
        if is_loaded and not rm.llm.wait_idle(0):
            raise HTTPException(409, "LLM 正在推理，稍后再重新加载")
        ttl = cfg.llm_session_ttl_s if actions.planner.mode == "on_demand" else None

        def job() -> None:
            if rm.llm.loaded():
                rm.llm.unload()
            rm.llm.load(ttl)

        threading.Thread(target=_llm_bg, args=(job,), daemon=True).start()
        return {"state": "working"}

    @app.post("/api/llm/unload")
    def llm_unload() -> dict:
        rm = studio.orch.resources
        if not rm.llm.loaded():
            return {"state": "unloaded"}
        if not rm.llm.wait_idle(0):
            raise HTTPException(409, "LLM 正在推理，稍后再卸载")
        guard(rm.llm.unload)
        return {"state": "unloaded"}

    # ---- W2: export to ComfyUI for editing, import back as a candidate variant, trial, promote
    # ----
    workshop = Workshop(studio)

    @app.get("/api/tasks/{task_id}/workflow")
    def export_workflow(task_id: str) -> Response:
        name, data = guard(lambda: workshop.export(task_id))
        return Response(
            data,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/api/variants/import")
    async def import_workflow(request: Request, base: str, task: str | None = None) -> dict:
        data = await request.body()
        t = store.get(task) if task else None
        return guard(lambda: workshop.import_graph(data, base, t and t["project_id"], task))

    @app.get("/api/variants")
    def variants(project: str | None = None) -> list[dict]:
        return workshop.list(project)

    @app.post("/api/variants/{vid}/trial")
    def variant_trial(vid: str) -> dict:
        return guard(lambda: workshop.trial(vid))

    @app.post("/api/variants/{vid}/promote")
    def variant_promote(vid: str, name: str = Body(...), title: str | None = Body(None)) -> dict:
        return guard(lambda: workshop.promote(vid, name, title))

    @app.post("/api/variants/{vid}/reject")
    def variant_reject(vid: str) -> dict:
        return guard(lambda: workshop.reject(vid))

    @app.get("/api/messages")
    def messages(project: str | None = None, after: int = 0) -> list[dict]:
        pid = project or studio.current_project()["id"]
        return [m for m in store.messages(pid) if m["id"] > after]

    # ---- files ----
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
