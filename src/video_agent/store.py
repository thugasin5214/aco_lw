"""任务持久化：SQLite 做索引，每任务目录下的 manifest.json 是可读副本。

所有状态变化都经 `transition()`，非法迁移直接抛错；每次变化写 events 并重写 manifest。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

STATES = [
    "draft",
    "validated",
    "waiting_memory",
    "submitted",
    "running",
    "review",
    "done",
    "rejected",
    "failed",
    "timeout",
]
TERMINAL = {"done", "rejected", "failed", "timeout"}
ACTIVE = {"submitted", "running"}
TRANSITIONS = {
    "draft": {"validated", "failed"},
    "validated": {"waiting_memory", "failed"},
    "waiting_memory": {"submitted", "failed", "timeout"},
    "submitted": {"running", "review", "failed", "timeout"},
    "running": {"review", "failed", "timeout"},
    "review": {"done", "rejected"},
    "timeout": {"review", "failed"},  # 超时只是停止观察；恢复时可能发现已完成
}
STATE_ZH = {
    "draft": "草稿",
    "validated": "已校验",
    "waiting_memory": "等待内存",
    "submitted": "已提交",
    "running": "生成中",
    "review": "待审阅",
    "done": "完成",
    "rejected": "打回",
    "failed": "失败",
    "timeout": "超时",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  state TEXT NOT NULL,
  template TEXT NOT NULL,
  template_sha TEXT NOT NULL,
  kind TEXT NOT NULL,
  tier TEXT NOT NULL,
  title TEXT,
  parent_id TEXT,
  params TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  prompt_id TEXT,
  outputs TEXT NOT NULL DEFAULT '[]',
  metrics TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_hash ON tasks(params_hash);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts REAL NOT NULL,
  state TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
"""

JSON_COLS = ("params", "outputs", "metrics")


class TransitionError(RuntimeError):
    pass


class Store:
    def __init__(self, db_path: Path, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- 读 ----
    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for c in JSON_COLS:
            d[c] = json.loads(d[c])
        return d

    def get(self, task_id: str) -> dict | None:
        return self._row(self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def by_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" * len(states))
        q = f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created"
        return [self._row(r) for r in self.db.execute(q, states)]

    def find_duplicate(self, params_hash: str) -> dict | None:
        """同参数且未失败的任务；失败/打回的可以重做（超时的可能仍在跑，不算）。"""
        r = self.db.execute(
            "SELECT * FROM tasks WHERE params_hash=? AND state NOT IN ('failed','rejected')"
            " ORDER BY created DESC LIMIT 1",
            (params_hash,),
        ).fetchone()
        return self._row(r)

    def events(self, task_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
        return [dict(r) for r in rows]

    def task_dir(self, task_id: str) -> Path:
        return self.jobs_dir / task_id

    # ---- 写 ----
    def create(
        self,
        *,
        template: str,
        template_sha: str,
        kind: str,
        tier: str,
        params: dict,
        params_hash: str,
        title: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        now = time.time()
        task_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self.db:
            self.db.execute(
                "INSERT INTO tasks (id, created, updated, state, template, template_sha, kind, tier,"
                " title, parent_id, params, params_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    now,
                    now,
                    "draft",
                    template,
                    template_sha,
                    kind,
                    tier,
                    title,
                    parent_id,
                    json.dumps(params, ensure_ascii=False),
                    params_hash,
                ),
            )
            self.db.execute(
                "INSERT INTO events (task_id, ts, state, note) VALUES (?,?,?,?)",
                (task_id, now, "draft", None),
            )
        self.task_dir(task_id).mkdir(parents=True, exist_ok=True)
        self.write_manifest(task_id)
        return self.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        for c in JSON_COLS:
            if c in fields:
                fields[c] = json.dumps(fields[c], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE tasks SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), task_id),
            )
        self.write_manifest(task_id)

    def transition(self, task_id: str, new: str, note: str | None = None, **fields) -> dict:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        old = task["state"]
        if new not in TRANSITIONS.get(old, set()):
            raise TransitionError(f"{task_id}: 非法迁移 {old} → {new}")
        now = time.time()
        for c in JSON_COLS:
            if c in fields:
                fields[c] = json.dumps(fields[c], ensure_ascii=False)
        cols = "".join(f", {k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE tasks SET state=?, updated=?{cols} WHERE id=? AND state=?",
                (new, now, *fields.values(), task_id, old),
            )
            self.db.execute(
                "INSERT INTO events (task_id, ts, state, note) VALUES (?,?,?,?)",
                (task_id, now, new, note),
            )
        self.write_manifest(task_id)
        return self.get(task_id)

    def write_manifest(self, task_id: str) -> None:
        task = self.get(task_id)
        task["events"] = self.events(task_id)
        path = self.task_dir(task_id) / "manifest.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2))
        tmp.replace(path)
