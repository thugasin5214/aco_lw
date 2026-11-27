"""Creative layer: projects, rounds of candidates, review decisions, the director's baseline pack.

Telegram, the web review console and the CLI only call functions here; this only writes the database and queues tasks (draft);
the actual ComfyUI submission is still done by the Worker through the orchestrator. Everything is deterministic; no LLM calls.
"""

from __future__ import annotations

import random
import shutil
from collections.abc import Callable
from pathlib import Path

from .i18n import tr
from .render import TIER_KEYS
from .scheduler import Orchestrator
from .store import Store

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}
RUNNING_STATES = {"draft", "validated", "waiting_memory", "submitted", "running"}


NO_PROJECT = "none"  # purge target for outputs submitted outside any project


class StudioError(ValueError):
    pass


class Studio:
    def __init__(self, orch: Orchestrator, round_size: int = 4):
        self.orch = orch
        self.store: Store = orch.store
        self.round_size = round_size
        # you decided on a round → notify (e.g. an agent run waiting for that round)
        self.decision_listeners: list = []
        # a new round started (including redo / refine children) → notify (e.g. production artifacts
        # follow it)
        self.round_listeners: list = []
        # interrupt one running task (injected by the daemon: the Worker's job in ComfyUI /
        # FreeVideo); returns whether it was the one running
        self.stop_task = lambda task_id: False
        # cancel one of our jobs in its executor (injected by the daemon)
        self.cancel_job: Callable[[dict], bool] = lambda task: False

    # ---- delete outputs (with local files) ----
    BUSY = frozenset({"draft", "validated", "waiting_memory", "submitted", "running"})
    RUN_DONE = frozenset({"done", "failed", "cancelled"})

    def _remove_files(self, task: dict) -> int:
        """Delete the task directory data/jobs/<ID>/ and the originals this system generated in ComfyUI output; returns bytes freed."""
        freed = 0
        jobs = self.store.jobs_dir.resolve()
        d = self.store.task_dir(task["id"]).resolve()
        if d.parent == jobs and d.is_dir():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
        root = getattr(self.orch.cfg, "comfy_output_dir", "") or ""
        if root:
            base = Path(root).expanduser().resolve()
            for o in task["outputs"] or []:
                src = o.get("source") or {}
                name, sub = src.get("filename") or "", src.get("subfolder") or ""
                # only our own: output type, aco_lw subfolder, filename starting with the task ID
                if (
                    src.get("type") != "output"
                    or sub != "aco_lw"
                    or not name.startswith(task["id"])
                ):
                    continue
                p = (base / sub / name).resolve()
                if p.is_relative_to(base) and p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
        return freed

    def _delete(self, tasks: list[dict], round_ids: list[str]) -> dict:
        busy = [t for t in tasks if t["state"] in self.BUSY]
        if busy:
            raise StudioError(tr("delete_busy", n=len(busy)))
        refs = self.store.active_production_refs(round_ids, [t["id"] for t in tasks])
        if refs:
            raise StudioError(tr("delete_in_production", p=refs[0]["production_title"]))
        freed = sum(self._remove_files(t) for t in tasks)
        self.store.delete_tasks([t["id"] for t in tasks])
        for rid in round_ids:
            self.store.delete_round_row(rid)
        return {"tasks": len(tasks), "rounds": len(round_ids), "freed_mb": round(freed / 2**20, 1)}

    def delete_round(self, round_id: str) -> dict:
        rnd = self.store.get_round(round_id)
        if rnd is None:
            raise StudioError(tr("no_round", r=round_id))
        out = self._delete(self.store.round_tasks(round_id), [round_id])
        self.log("system", "out", tr("log_deleted", **out), rnd["project_id"])
        return out

    def delete_task(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(tr("no_task", t=task_id))
        rid = task["round_id"]
        rest = [t for t in self.store.round_tasks(rid) if t["id"] != task_id] if rid else []
        out = self._delete([task], [rid] if rid and not rest else [])
        self.log("system", "out", tr("log_deleted", **out), task["project_id"])
        return out

    def purge_project(self, project_id: str, scope: str = "review") -> dict:
        """Clean up a project's outputs. scope=review: only what awaits review; unselected: everything not approved
        (review / rejected / failed / timeout). Approved, running and active-production items are kept."""
        if scope not in ("review", "unselected"):
            raise StudioError("scope must be review or unselected")
        states = {"review"} if scope == "review" else {"review", "rejected", "failed", "timeout"}
        if project_id == NO_PROJECT:  # outputs submitted outside any project (CLI / tests)
            all_tasks = [t for t in self.store.list(1_000_000) if not t["project_id"]]
            review_rounds = {
                r["id"] for r in self.store.rounds(None, "review") if not r["project_id"]
            }
            project_id = None
        else:
            all_tasks = self.store.list(1_000_000, project_id)
            # the review page shows "rounds in review": their candidates may all have failed,
            # delete those too
            review_rounds = {r["id"] for r in self.store.rounds(project_id, "review")}
        tasks = [
            t
            for t in all_tasks
            if t["state"] in states
            or (t["round_id"] in review_rounds and t["state"] not in self.BUSY | {"done"})
        ]
        rounds = {t["round_id"] for t in tasks if t["round_id"]}
        used = self.store.active_production_refs(list(rounds), [t["id"] for t in tasks])
        # the version a production uses (current + task_id): keep only the chosen candidate, delete
        # its unselected siblings;
        # versions still in review / generating: keep the whole round
        skip_r = {
            a["round_id"]
            for a in used
            if a["round_id"] and not (a["status"] == "current" and a["task_id"])
        }
        skip_t = {a["task_id"] for a in used if a["task_id"]}
        tasks = [t for t in tasks if t["id"] not in skip_t and t["round_id"] not in skip_r]
        ids = {t["id"] for t in tasks}
        empty = [
            r for r in rounds - skip_r if all(t["id"] in ids for t in self.store.round_tasks(r))
        ]
        out = self._delete(tasks, empty)
        out["kept_for_productions"] = len(skip_r) + len(skip_t)
        out["kept_productions"] = sorted({a["production_title"] for a in used})
        text = tr("log_deleted", **out)
        if out["kept_for_productions"]:
            text += tr(
                "log_kept", n=out["kept_for_productions"], p="、".join(out["kept_productions"])
            )
        if project_id:
            self.log("system", "out", text, project_id)
        return out

    def delete_project(self, project_id: str, delete_production=None) -> dict:
        """Delete a project for real: its productions (through `delete_production`), every task
        and round with their files, baseline packs, sessions, messages, agent runs and memories.
        Refused while the agent works in it or ComfyUI / FreeVideo runs one of its tasks."""
        p = self.store.get_project(project_id)
        if p is None:
            raise StudioError(f"no project {project_id}")
        live = [
            r for r in self.store.runs_of_project(project_id) if r["status"] not in self.RUN_DONE
        ]
        if live:
            raise StudioError(tr("session_busy", n=len(live)))
        tasks = self.store.list(1_000_000, project_id)
        # drafts are cancelled; only what ComfyUI / FreeVideo is already working on blocks
        busy = [t for t in tasks if t["state"] in ("submitted", "running")]
        if busy:
            raise StudioError(tr("delete_busy", n=len(busy)))
        prods = self.store.productions(project_id)
        for prod in prods:  # nothing of it starts any more while we delete
            self.store.update_production(prod["id"], state="paused")
        self.cancel_queued(project_id, "project deleted")
        for prod in prods:
            if delete_production is None:
                raise StudioError("production engine not running")
            if self.store.get_production(prod["id"]):  # episodes go with their series
                delete_production(prod["id"])
        tasks = self.store.list(1_000_000, project_id)
        freed = sum(self._remove_files(t) for t in tasks)
        self.store.delete_tasks([t["id"] for t in tasks])
        rounds = self.store.rounds(project_id)
        for r in rounds:
            self.store.delete_round_row(r["id"])
        shutil.rmtree(self.store.packs_dir / project_id, ignore_errors=True)
        rows = self.store.delete_project_rows(project_id)
        if self.store.get_kv("current_project") == project_id:
            self.store.set_kv("current_project", None)
        return {
            **rows,
            "name": p["name"],
            "productions": len(prods),
            "tasks": len(tasks),
            "rounds": len(rounds),
            "freed_mb": round(freed / 2**20, 1),
        }

    # ---- projects ----
    def current_project(self) -> dict:
        pid = self.store.get_kv("current_project")
        p = pid and self.store.get_project(pid)
        if p and p["state"] == "active":
            return p
        actives = [p for p in self.store.list_projects() if p["state"] == "active"]
        p = actives[0] if actives else self.store.create_project(tr("default_project"))
        self.store.set_kv("current_project", p["id"])
        return p

    def new_project(self, name: str, note: str | None = None) -> dict:
        p = self.store.create_project(name, note)
        self.store.set_kv("current_project", p["id"])
        self.log("system", "out", tr("log_project_new", n=name), p["id"])
        return p

    def use_project(self, key: str) -> dict:
        p = self.store.find_project(key)
        if p is None:
            raise StudioError(f"找不到项目 {key}")
        if p["state"] != "active":
            p = self.store.update_project(p["id"], state="active")
        self.store.set_kv("current_project", p["id"])
        return p

    def finish_project(self, project_id: str | None = None, note: str | None = None) -> dict:
        """Mark done: kept forever and still browsable in the console; queued tasks are cancelled."""
        pid = project_id or self.current_project()["id"]
        self.cancel_queued(pid, "项目已完成")
        p = self.store.update_project(pid, state="done", note=note)
        self.log("system", "out", tr("log_project_done", n=p["name"]), pid)
        if self.store.get_kv("current_project") == pid:
            self.store.set_kv("current_project", None)
        return p

    def log(
        self,
        channel: str,
        direction: str,
        text: str,
        project_id: str | None = None,
        author: str | None = None,
        **meta,
    ) -> int:
        pid = project_id or self.current_project()["id"]
        sid = self.current_session(pid)["id"]
        return self.store.log_message(channel, direction, text, pid, author, session_id=sid, **meta)

    # ---- sessions ----
    def current_session(self, project_id: str | None = None) -> dict:
        """The project's current session; created if there is none. Messages from old versions without sessions go into a "history session"."""
        pid = project_id or self.current_project()["id"]
        sid = self.store.get_kv(f"session:{pid}")
        s = sid and self.store.get_session(sid)
        if s and s["state"] == "open":
            return s
        self._adopt_history(pid)
        existing = [x for x in self.store.sessions(pid) if x["state"] == "open"]
        s = existing[0] if existing else self.store.create_session(pid, tr("session_n", n=1))
        self.store.set_kv(f"session:{pid}", s["id"])
        return s

    def _adopt_history(self, pid: str) -> None:
        """Messages from old versions without a session all go into one "history session"."""
        if self.store.db.execute(
            "SELECT 1 FROM messages WHERE project_id=? AND session_id IS NULL LIMIT 1", (pid,)
        ).fetchone():
            h = self.store.create_session(pid, tr("history_session"))
            self.store.adopt_orphan_messages(pid, h["id"])

    def new_session(self, title: str | None = None, project_id: str | None = None) -> dict:
        pid = project_id or self.current_project()["id"]
        self._adopt_history(pid)
        n = len(self.store.sessions(pid)) + 1
        s = self.store.create_session(pid, title or tr("session_n", n=n))
        self.store.set_kv(f"session:{pid}", s["id"])
        return s

    def use_session(self, key: str, project_id: str | None = None) -> dict:
        """Switch session by ID or list index (1 = most recently active)."""
        pid = project_id or self.current_project()["id"]
        rows = self.store.sessions(pid)
        s = (
            self.store.get_session(key)
            if not key.isdigit()
            else (rows[int(key) - 1] if 0 < int(key) <= len(rows) else None)
        )
        if s is None or s["project_id"] != pid:
            raise StudioError(f"当前项目里没有会话 {key}")
        if s["state"] != "open":
            s = self.store.update_session(s["id"], state="open")
        self.store.set_kv(f"session:{pid}", s["id"])
        return s

    def delete_session(self, key: str, project_id: str | None = None) -> dict:
        """Delete a session (by ID or list index). Refused while the agent is working in it; deleting the current session switches to the next one."""
        if project_id is None and not key.isdigit():
            s = self.store.get_session(key)
            project_id = s and s["project_id"]
        pid = project_id or self.current_project()["id"]
        rows = self.store.sessions(pid)
        s = (
            self.store.get_session(key)
            if not key.isdigit()
            else (rows[int(key) - 1] if 0 < int(key) <= len(rows) else None)
        )
        if s is None or s["project_id"] != pid:
            raise StudioError(f"当前项目里没有会话 {key}")
        live = [r for r in self.store.runs(s["id"]) if r["status"] not in self.RUN_DONE]
        if live:
            raise StudioError(tr("session_busy", n=len(live)))
        out = self.store.delete_session(s["id"])
        if self.store.get_kv(f"session:{pid}") == s["id"]:
            self.store.set_kv(f"session:{pid}", None)
        return {**out, "title": s["title"], "current": self.current_session(pid)}

    # ---- project memory (proposed by the LLM, applied after the user confirms) ----
    def propose_memories(
        self, texts: list[str], source: dict, project_id: str | None = None
    ) -> list[dict]:
        pid = project_id or self.current_project()["id"]
        have = {m["text"] for m in self.store.memories(pid, "active", "proposed")}
        return [
            self.store.add_memory(pid, t.strip(), source=source)
            for t in texts
            if t.strip() and t.strip() not in have
        ]

    def add_memory(self, text: str, project_id: str | None = None) -> dict:
        pid = project_id or self.current_project()["id"]
        return self.store.add_memory(pid, text.strip(), status="active", source={"by": "user"})

    def set_memory(self, memory_id: int, action: str, text: str | None = None) -> dict:
        """accept / ignore / archive / restore / pin / unpin / edit (edit = a new version supersedes the old)."""
        m = self.store.get_memory(memory_id)
        if m is None:
            raise StudioError(f"找不到记忆 {memory_id}")
        if action == "accept":
            return self.store.update_memory(memory_id, status="active")
        if action in ("ignore", "archive"):
            return self.store.update_memory(memory_id, status="archived")
        if action == "restore":
            return self.store.update_memory(memory_id, status="active")
        if action in ("pin", "unpin"):
            return self.store.update_memory(memory_id, pinned=int(action == "pin"))
        if action == "edit" and text:
            self.store.update_memory(memory_id, status="archived")
            return self.store.add_memory(
                m["project_id"],
                text.strip(),
                m["category"],
                "active",
                {"by": "user", "edited": memory_id},
                memory_id,
            )
        raise StudioError(f"未知记忆操作 {action}")

    # ---- uploaded reference images ----
    def save_upload(self, name: str, data: bytes, project_id: str | None = None) -> dict:
        pid = project_id or self.current_project()["id"]
        ext = Path(name).suffix.lower() or ".png"
        if ext not in IMAGE_EXT | VIDEO_EXT:
            raise StudioError(f"不支持的文件类型 {ext}")
        d = self.store.jobs_dir.parent / "uploads" / pid
        d.mkdir(parents=True, exist_ok=True)
        up = self.store.add_upload(pid, name, "", "video" if ext in VIDEO_EXT else "image")
        path = d / f"{up['id']}{ext}"
        path.write_bytes(data)
        with self.store._lock, self.store.db:
            self.store.db.execute("UPDATE uploads SET path=? WHERE id=?", (str(path), up["id"]))
        return self.store.get_upload(up["id"])

    # ---- manual tweaks (W1, no LLM) ----
    def tweak(
        self,
        task_id: str,
        params: dict,
        tier: str | None = None,
        n: int | None = None,
        origin: str = "web",
    ) -> dict:
        """Start a new round from one task's parameters with the user's overrides; the DAG links to the original task."""
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(f"找不到任务 {task_id}")
        base = {k: v for k, v in task["params"].items() if k not in TIER_KEYS}
        base.update(params)
        seed = base.pop("seed", None)
        n = n or 1
        seeds = [seed + i for i in range(n)] if isinstance(seed, int) else None
        rnd = self.start_round(
            task["template"],
            base,
            n,
            tier or task["tier"],
            (task["title"] or task["template"]).split(" #")[0] + " · tweak",
            origin=origin,
            request="manual tweak",
            project_id=task["project_id"],
            seeds=seeds,
            allow_unverified=True,
            parent_task=task_id,
        )
        changed = ", ".join(params) or tr("log_no_change")
        self.log(
            origin,
            "in",
            tr("log_tweak", id=task_id, changed=changed),
            task["project_id"],
            round_id=rnd["id"],
        )
        return rnd

    # ---- rounds of candidates ----
    def start_round(
        self,
        template: str,
        params: dict,
        n: int | None = None,
        tier: str | None = None,
        title: str | None = None,
        origin: str = "cli",
        request: str | None = None,
        parent_round: str | None = None,
        project_id: str | None = None,
        seeds: list[int] | None = None,
        allow_unverified: bool = False,
        parent_task: str | None = None,
        priority: int | None = None,
    ) -> dict:
        t = self.orch.registry.template(template)
        pid = project_id or self.current_project()["id"]
        # started by you → interactive; started by the agent itself → 1; trials and other background
        # → 0
        if priority is None:
            priority = {"agent": 1, "background": 0}.get(origin, 2)
        base = {k: v for k, v in params.items() if k != "seed" and k not in TIER_KEYS}
        if n is None:
            n = 1 if t.kind == "video" else self.round_size
        if seeds is None:
            start = params.get("seed", random.randrange(1, 2**31 - 1000))
            seeds = [start + i for i in range(n)]
        n = len(seeds)
        rnd = self.store.create_round(
            project_id=pid,
            template=t.name,
            tier=tier,
            title=title,
            base_params=base,
            n=n,
            parent_round=parent_round,
            origin=origin,
            request=request,
        )
        try:
            for i, seed in enumerate(seeds):
                cand = {**base, "seed": seed} if "seed" in t.slots else dict(base)
                task = self.orch.create(
                    t.name,
                    cand,
                    tier,
                    title=f"{title or t.title} #{i + 1}",
                    allow_unverified=allow_unverified,
                    allow_duplicate=True,
                    project_id=pid,
                    round_id=rnd["id"],
                    idx=i + 1,
                    parent_id=parent_task,
                    priority=priority,
                )
                if i == 0 and tier is None:
                    self.store.update_round(rnd["id"], tier=task["tier"])
        except Exception:
            self.cancel_round(rnd["id"], "创建失败")
            raise
        rnd = self.store.get_round(rnd["id"])
        for cb in self.round_listeners:
            cb(rnd)
        return rnd

    def refresh_round(self, round_id: str) -> tuple[dict, bool]:
        """When every candidate has finished, the round goes to review; returns (round, whether it just entered review)."""
        rnd = self.store.get_round(round_id)
        if rnd["state"] != "generating":
            return rnd, False
        tasks = self.store.round_tasks(round_id)
        if any(t["state"] in RUNNING_STATES for t in tasks):
            return rnd, False
        return self.store.update_round(round_id, state="review"), True

    def cancel_round(self, round_id: str, reason: str) -> None:
        for t in self.store.round_tasks(round_id):
            if t["state"] == "draft":
                self.store.transition(t["id"], "failed", f"已取消：{reason}", error="cancelled")
        closed = self.store.update_round(
            round_id, state="closed", decision={"action": "cancel", "note": reason}
        )
        for cb in self.decision_listeners:  # agent runs waiting for this round get the result too
            cb(closed)

    PENDING_DELETE = "delete_rounds"  # kv: rounds to delete once their running render stops

    def stop_round(self, round_id: str, delete: bool = False) -> dict:
        """Stop a round halfway: candidates not started are dropped, the running one is
        interrupted. Finished candidates stay for review — or, with delete, the whole round goes
        (right away, or as soon as the interrupted render has stopped)."""
        rnd = self.store.get_round(round_id)
        if rnd is None:
            raise StudioError(tr("no_round", r=round_id))
        tasks = self.store.round_tasks(round_id)
        if delete:
            refs = self.store.active_production_refs([round_id], [t["id"] for t in tasks])
            if refs:
                raise StudioError(tr("delete_in_production", p=refs[0]["production_title"]))
        dropped = 0
        for t in tasks:
            if t["state"] == "draft":
                self.store.transition(t["id"], "failed", tr("task_stopped"), error="cancelled")
                dropped += 1
        live = [t for t in self.store.round_tasks(round_id) if t["state"] in self.BUSY]
        interrupted = [t["id"] for t in live if self.stop_task(t["id"])]
        # renders our watch gave up on (timeout) that ComfyUI is still working on: no Worker
        # holds them any more, so cancel our job in the executor directly
        for t in self.store.round_tasks(round_id):
            if t["state"] == "timeout" and t["prompt_id"] and self.cancel_job(t):
                self.store.transition(t["id"], "failed", tr("task_stopped"), error="cancelled")
                interrupted.append(t["id"])
        out = {"round_id": round_id, "dropped": dropped, "interrupted": len(interrupted)}
        if delete:
            closed = self.store.update_round(
                round_id, state="closed", decision={"action": "cancel", "note": tr("task_stopped")}
            )
            for cb in self.decision_listeners:  # agent runs waiting for this round continue
                cb(closed)
            if live:  # the Worker deletes it once the interrupted render has stopped
                rows = self.store.get_kv(self.PENDING_DELETE) or []
                self.store.set_kv(self.PENDING_DELETE, [*rows, round_id])
                return {**out, "deleted": False, "deleting": True}
            return {**out, **self.delete_round(round_id), "deleted": True}
        self.refresh_round(round_id)
        return out

    def finish_pending_deletes(self) -> int:
        """Delete stopped rounds whose renders have all ended (called by the Worker)."""
        rows = self.store.get_kv(self.PENDING_DELETE) or []
        if not rows:
            return 0
        keep, n = [], 0
        for rid in rows:
            if self.store.get_round(rid) is None:
                continue
            if any(t["state"] in self.BUSY for t in self.store.round_tasks(rid)):
                keep.append(rid)
                continue
            try:
                self.delete_round(rid)
                n += 1
            except StudioError:
                pass  # e.g. a production picked it up meanwhile: leave it
        self.store.set_kv(self.PENDING_DELETE, keep)
        return n

    def cancel_queued(self, project_id: str | None = None, reason: str = "用户取消") -> int:
        n = 0
        for t in self.store.by_state("draft"):
            if project_id is None or t["project_id"] == project_id:
                self.store.transition(t["id"], "failed", f"已取消：{reason}", error="cancelled")
                n += 1
        for r in self.store.rounds(project_id, "generating"):
            self.refresh_round(r["id"])
        return n

    # ---- review decisions ----
    def _review_round(self, round_id: str) -> tuple[dict, list[dict]]:
        rnd = self.store.get_round(round_id)
        if rnd is None:
            raise StudioError(f"找不到轮 {round_id}")
        if rnd["state"] == "closed":
            raise StudioError("这一轮已经做过决定")
        if rnd["state"] != "review":
            raise StudioError("这一轮还在生成，等全部候选出来再审阅")
        return rnd, self.store.round_tasks(round_id)

    def _close(
        self, rnd: dict, tasks: list[dict], keep: str | None, decision: dict, note: str | None
    ) -> dict:
        for t in tasks:
            if t["state"] != "review":
                continue
            if t["id"] == keep:
                self.store.transition(t["id"], "done", note or "选中")
            else:
                self.store.transition(t["id"], "rejected", decision["action"])
        prev = (rnd.get("decision") or {}).get("history")
        if prev:  # a round reviewed again: keep the earlier decision history
            decision = {**decision, "history": prev}
        closed = self.store.update_round(rnd["id"], state="closed", decision=decision)
        for cb in self.decision_listeners:
            cb(closed)
        return closed

    def _task_in_round(self, tasks: list[dict], key: str | int) -> dict:
        for t in tasks:
            if t["id"] == key or str(t["idx"]) == str(key):
                return t
        raise StudioError(f"这一轮里没有候选 {key}")

    def select(
        self,
        round_id: str,
        which: str | int,
        role: str | None = None,
        note: str | None = None,
        origin: str = "web",
    ) -> dict:
        """Approve: pick one candidate; with a role, lock it into the project's baseline pack."""
        rnd, tasks = self._review_round(round_id)
        task = self._task_in_round(tasks, which)
        decision = {
            "action": "select",
            "task_id": task["id"],
            "idx": task["idx"],
            "role": role,
            "note": note,
            "origin": origin,
        }
        rnd = self._close(rnd, tasks, task["id"], decision, note)
        if role:
            self.lock(task["id"], role, note, rnd["project_id"])
        self.log(
            origin,
            "in",
            tr("log_select", idx=task["idx"], lock=tr("log_lock_suffix", r=role) if role else ""),
            rnd["project_id"],
        )
        return rnd

    def redo(self, round_id: str, origin: str = "web", n: int | None = None) -> dict:
        """Redo: reject the whole round and run another with the same parameters and new seeds."""
        rnd, tasks = self._review_round(round_id)
        self._close(rnd, tasks, None, {"action": "redo", "origin": origin}, None)
        self.log(origin, "in", tr("log_redo"), rnd["project_id"])
        return self.start_round(
            rnd["template"],
            rnd["base_params"],
            n or rnd["n"],
            rnd["tier"],
            rnd["title"],
            origin=origin,
            parent_round=rnd["id"],
            project_id=rnd["project_id"],
            allow_unverified=True,
        )

    def refine(
        self,
        round_id: str,
        which: str | int,
        delta: dict,
        note: str | None = None,
        origin: str = "web",
    ) -> dict:
        """Refine: right direction; keep that candidate's seed and parameters, apply a parameter delta and run another round."""
        if not delta:
            raise StudioError("微调需要至少一个参数改动")
        rnd, tasks = self._review_round(round_id)
        base_task = self._task_in_round(tasks, which)
        decision = {
            "action": "refine",
            "task_id": base_task["id"],
            "idx": base_task["idx"],
            "delta": delta,
            "note": note,
            "origin": origin,
        }
        self._close(rnd, tasks, None, decision, note)
        self.log(
            origin,
            "in",
            tr("log_refine", idx=base_task["idx"], note=note or delta),
            rnd["project_id"],
        )
        params = {k: v for k, v in base_task["params"].items() if k not in TIER_KEYS}
        params.update(delta)
        seed = params.pop("seed", None)
        n = rnd["n"]
        seeds = [seed] + [random.randrange(1, 2**31) for _ in range(n - 1)] if seed else None
        return self.start_round(
            rnd["template"],
            params,
            n,
            rnd["tier"],
            rnd["title"],
            origin=origin,
            request=note,
            parent_round=rnd["id"],
            project_id=rnd["project_id"],
            seeds=seeds,
            allow_unverified=True,
        )

    def reject(self, round_id: str, note: str | None = None, origin: str = "web") -> dict:
        """Reject: drop the whole round; with a note, the planner turns it into a parameter delta and starts another round (see planner)."""
        rnd, tasks = self._review_round(round_id)
        rnd = self._close(
            rnd, tasks, None, {"action": "reject", "note": note, "origin": origin}, note
        )
        self.log(origin, "in", tr("log_reject", note=note or tr("log_no_note")), rnd["project_id"])
        return rnd

    def approve_task(
        self, task_id: str, accept: bool, note: str | None = None, origin: str = "web"
    ) -> dict:
        """Approve / reject a single task that belongs to no round (e.g. submitted from the CLI)."""
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(f"找不到任务 {task_id}")
        if task["round_id"]:
            rnd = self.store.get_round(task["round_id"])
            if accept:
                return self.select(rnd["id"], task_id, note=note, origin=origin)
            return self.reject(rnd["id"], note, origin)
        if task["state"] != "review":
            raise StudioError(f"任务不在待审阅状态（{task['state']}）")
        return self.orch.review(task_id, accept, note)

    def redo_task(self, task_id: str, origin: str = "web") -> dict:
        """Redo a single task: reject it and start a round with the same parameters (new seed)."""
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(f"找不到任务 {task_id}")
        if task["round_id"]:
            return self.redo(task["round_id"], origin)
        if task["state"] == "review":
            self.orch.review(task_id, False, "重做")
        params = {k: v for k, v in task["params"].items() if k not in TIER_KEYS}
        return self.start_round(
            task["template"],
            params,
            None,
            task["tier"],
            task["title"],
            origin=origin,
            project_id=task["project_id"],
            allow_unverified=True,
        )

    def reopen(self, round_id: str, origin: str = "web") -> tuple[dict, int]:
        """Change a review decision: a decided round goes back to review. The old decision goes into history; if it was locked into the baseline pack,
        a new pack version restores that role to the asset it had before the lock. Returns (round, number of child rounds still generating)."""
        rnd = self.store.get_round(round_id)
        if rnd is None:
            raise StudioError(f"round {round_id} not found")
        d = rnd["decision"] or {}
        if rnd["state"] != "closed" or d.get("action") not in (
            "select",
            "redo",
            "refine",
            "reject",
        ):
            raise StudioError(tr("err_not_reopenable"))
        for t in self.store.round_tasks(round_id):
            if t["state"] in ("done", "rejected"):
                self.store.transition(t["id"], "review", tr("log_reopen"))
        if d.get("action") == "select" and d.get("role"):
            self._unlock(rnd["project_id"], d["role"], d["task_id"])
        history = [*d.get("history", []), {k: v for k, v in d.items() if k != "history"}]
        rnd = self.store.update_round(round_id, state="review", decision={"history": history})
        children = [
            r
            for r in self.store.rounds(rnd["project_id"])
            if r["parent_round"] == round_id and r["state"] == "generating"
        ]
        self.log(
            origin,
            "in",
            tr("log_reopen_round", t=rnd["title"] or rnd["template"]),
            rnd["project_id"],
            round_id=round_id,
        )
        return rnd, len(children)

    def reopen_task(self, task_id: str, origin: str = "web") -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(f"task {task_id} not found")
        if task["round_id"]:
            return self.reopen(task["round_id"], origin)[0]
        if task["state"] not in ("done", "rejected"):
            raise StudioError(tr("err_not_reopenable"))
        return self.store.transition(task_id, "review", tr("log_reopen"))

    def _unlock(self, pid: str, role: str, task_id: str) -> None:
        """Undo a lock: in a new version the role goes back to the last asset that was not this task; removed if there is none."""
        cur = self.store.pack(pid)
        if not cur or cur["assets"].get(role, {}).get("task_id") != task_id:
            return
        previous = None
        for v in self.store.pack_history(pid)[1:]:
            a = v["assets"].get(role)
            if a and a["task_id"] != task_id:
                previous = a
                break
        assets = dict(cur["assets"])
        if previous:
            assets[role] = previous
        else:
            assets.pop(role)
        self.store.add_pack_version(pid, assets, tr("log_unlock", r=role))

    # ---- director's baseline pack ----
    def lock(
        self, task_id: str, role: str, note: str | None = None, project_id: str | None = None
    ) -> dict:
        task = self.store.get(task_id)
        pid = project_id or task["project_id"] or self.current_project()["id"]
        out = next((o for o in task["outputs"]), None)
        if out is None:
            raise StudioError("该任务没有产物可锁定")
        cur = self.store.pack(pid)
        version = (cur["version"] if cur else 0) + 1
        dest_dir = self.store.packs_dir / pid / f"v{version}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = self.store.task_dir(task_id) / out["file"]
        dest = dest_dir / f"{role}{Path(out['file']).suffix}"
        shutil.copy2(src, dest)
        assets = dict(cur["assets"]) if cur else {}
        assets[role] = {
            "file": str(dest),
            "task_id": task_id,
            "kind": out["kind"],
            "template": task["template"],
            "seed": task["params"].get("seed"),
            "params": {k: v for k, v in task["params"].items() if k != "image"},
        }
        pack = self.store.add_pack_version(pid, assets, note or f"锁定 {role} ← {task_id}")
        self.log("system", "out", tr("log_pack", v=pack["version"], r=role), pid)
        return pack
