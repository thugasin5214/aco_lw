"""创作层：项目、候选轮、审阅决定、导演基准包。

Telegram、Web 审阅台、CLI 都只调用这里的函数；这里只写数据库和排队任务（draft），
真正提交 ComfyUI 仍由 Worker 经编排器完成。全部是确定性操作，不调用 LLM。
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from .render import TIER_KEYS
from .scheduler import Orchestrator
from .store import Store

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}
RUNNING_STATES = {"draft", "validated", "waiting_memory", "submitted", "running"}


class StudioError(ValueError):
    pass


class Studio:
    def __init__(self, orch: Orchestrator, round_size: int = 4):
        self.orch = orch
        self.store: Store = orch.store
        self.round_size = round_size

    # ---- 项目 ----
    def current_project(self) -> dict:
        pid = self.store.get_kv("current_project")
        p = pid and self.store.get_project(pid)
        if p and p["state"] == "active":
            return p
        actives = [p for p in self.store.list_projects() if p["state"] == "active"]
        p = actives[0] if actives else self.store.create_project("默认项目")
        self.store.set_kv("current_project", p["id"])
        return p

    def new_project(self, name: str, note: str | None = None) -> dict:
        p = self.store.create_project(name, note)
        self.store.set_kv("current_project", p["id"])
        self.log("system", "out", f"新建并切换到项目「{name}」", p["id"])
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
        """标记完成：永久保留，控制台仍可浏览；排队中的任务一并取消。"""
        pid = project_id or self.current_project()["id"]
        self.cancel_queued(pid, "项目已完成")
        p = self.store.update_project(pid, state="done", note=note)
        self.log("system", "out", f"项目「{p['name']}」已标记完成", pid)
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

    # ---- 会话 ----
    def current_session(self, project_id: str | None = None) -> dict:
        """项目的当前会话；没有则新建。旧版本没有会话的消息归入“历史会话”。"""
        pid = project_id or self.current_project()["id"]
        sid = self.store.get_kv(f"session:{pid}")
        s = sid and self.store.get_session(sid)
        if s and s["state"] == "open":
            return s
        existing = [x for x in self.store.sessions(pid) if x["state"] == "open"]
        if existing:
            s = existing[0]
        else:
            s = self.store.create_session(pid, "历史会话")
            if not self.store.adopt_orphan_messages(pid, s["id"]):
                s = self.store.update_session(s["id"], title="会话 1")
        self.store.set_kv(f"session:{pid}", s["id"])
        return s

    def new_session(self, title: str | None = None, project_id: str | None = None) -> dict:
        pid = project_id or self.current_project()["id"]
        n = len(self.store.sessions(pid)) + 1
        s = self.store.create_session(pid, title or f"会话 {n}")
        self.store.set_kv(f"session:{pid}", s["id"])
        return s

    def use_session(self, key: str, project_id: str | None = None) -> dict:
        """按 ID 或列表序号（1 = 最近活跃）切换会话。"""
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

    # ---- 项目记忆（LLM 提议，用户确认后生效） ----
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
        """accept / ignore / archive / restore / pin / unpin / edit（编辑 = 新版本取代旧版本）。"""
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

    # ---- 上传的参考图 ----
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

    # ---- 手动微调（W1，不经 LLM） ----
    def tweak(
        self,
        task_id: str,
        params: dict,
        tier: str | None = None,
        n: int | None = None,
        origin: str = "web",
    ) -> dict:
        """以某个任务的参数为底，覆盖用户改动后开新一轮；DAG 连到原任务。"""
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
            (task["title"] or task["template"]).split(" #")[0] + " · 手动微调",
            origin=origin,
            request="手动微调",
            project_id=task["project_id"],
            seeds=seeds,
            allow_unverified=True,
            parent_task=task_id,
        )
        changed = ", ".join(f"{k}" for k in params) or "无参数改动"
        self.log(
            origin, "in", f"手动微调 {task_id}：{changed}", task["project_id"], round_id=rnd["id"]
        )
        return rnd

    # ---- 候选轮 ----
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
    ) -> dict:
        t = self.orch.registry.template(template)
        pid = project_id or self.current_project()["id"]
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
                )
                if i == 0 and tier is None:
                    self.store.update_round(rnd["id"], tier=task["tier"])
        except Exception:
            self.cancel_round(rnd["id"], "创建失败")
            raise
        return self.store.get_round(rnd["id"])

    def refresh_round(self, round_id: str) -> tuple[dict, bool]:
        """全部候选跑完则进入待审阅；返回 (轮, 是否刚刚进入待审阅)。"""
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
        self.store.update_round(
            round_id, state="closed", decision={"action": "cancel", "note": reason}
        )

    def cancel_queued(self, project_id: str | None = None, reason: str = "用户取消") -> int:
        n = 0
        for t in self.store.by_state("draft"):
            if project_id is None or t["project_id"] == project_id:
                self.store.transition(t["id"], "failed", f"已取消：{reason}", error="cancelled")
                n += 1
        for r in self.store.rounds(project_id, "generating"):
            self.refresh_round(r["id"])
        return n

    # ---- 审阅决定 ----
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
        return self.store.update_round(rnd["id"], state="closed", decision=decision)

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
        """通过：选中一个候选；给了 role 就锁进项目基准包。"""
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
            f"选中 #{task['idx']}" + (f"，锁定为「{role}」" if role else ""),
            rnd["project_id"],
        )
        return rnd

    def redo(self, round_id: str, origin: str = "web", n: int | None = None) -> dict:
        """重做：整轮不要，同参数换种子再来一轮。"""
        rnd, tasks = self._review_round(round_id)
        self._close(rnd, tasks, None, {"action": "redo", "origin": origin}, None)
        self.log(origin, "in", "整轮重做（换种子）", rnd["project_id"])
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
        """微调：方向对，保留该候选的种子与参数，叠加参数增量后再出一轮。"""
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
        self.log(origin, "in", f"微调 #{base_task['idx']}：{note or delta}", rnd["project_id"])
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
        """打回：整轮不要；有批注时由规划器翻译成参数增量后另开一轮（见 planner）。"""
        rnd, tasks = self._review_round(round_id)
        rnd = self._close(
            rnd, tasks, None, {"action": "reject", "note": note, "origin": origin}, note
        )
        self.log(origin, "in", f"打回：{note or '（无批注）'}", rnd["project_id"])
        return rnd

    def approve_task(
        self, task_id: str, accept: bool, note: str | None = None, origin: str = "web"
    ) -> dict:
        """不属于任何轮的单个任务（例如 CLI 提交的）直接通过 / 打回。"""
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
        """单个任务重做：打回它，并以相同参数（换种子）开一轮。"""
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

    # ---- 导演基准包 ----
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
        self.log("system", "out", f"基准包 v{pack['version']}：锁定「{role}」", pid)
        return pack
