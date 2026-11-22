"""Persistence: SQLite is the index; manifest.json in each task directory is a readable copy.

Hierarchy: project → round of candidates → task; baseline packs and chat messages hang under the project.
Every task state change goes through `transition()`; illegal transitions raise; each change writes events and rewrites the manifest.
One connection is shared by several daemon threads; all access is serialized with a re-entrant lock.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
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
# done / rejected can go back to review via reopen
TERMINAL = {"done", "rejected", "failed", "timeout"}
ACTIVE = {"submitted", "running"}
TRANSITIONS = {
    "draft": {"validated", "failed"},
    "validated": {"waiting_memory", "failed"},
    "waiting_memory": {"submitted", "failed", "timeout"},
    "submitted": {"running", "review", "failed", "timeout"},
    "running": {"review", "failed", "timeout"},
    "review": {"done", "rejected"},
    "done": {"review"},  # a review decision can be changed: reopen
    "rejected": {"review"},
    # timeout only means we stopped watching; recovery may find it finished
    "timeout": {"review", "failed"},
}
STATE_ZH = {
    "draft": "排队",
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
ROUND_ZH = {"generating": "生成中", "review": "待审阅", "closed": "已决定"}
PROJECT_ZH = {"active": "进行中", "done": "已完成", "archived": "已归档"}

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
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  note TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'generating',
  template TEXT NOT NULL,
  tier TEXT,
  title TEXT,
  base_params TEXT NOT NULL DEFAULT '{}',
  n INTEGER NOT NULL,
  parent_round TEXT,
  origin TEXT,
  request TEXT,
  decision TEXT NOT NULL DEFAULT '{}',
  notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rounds_project ON rounds(project_id);
CREATE TABLE IF NOT EXISTS packs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  created REAL NOT NULL,
  assets TEXT NOT NULL,
  note TEXT,
  UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT,
  ts REAL NOT NULL,
  channel TEXT NOT NULL,
  direction TEXT NOT NULL,
  author TEXT,
  text TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS messages_project ON messages(project_id);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  title TEXT,
  summary TEXT NOT NULL DEFAULT '',
  summary_upto INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  text TEXT NOT NULL,
  category TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  pinned INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '{}',
  supersedes INTEGER
);
CREATE INDEX IF NOT EXISTS memories_project ON memories(project_id);
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  session_id TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  status TEXT NOT NULL,
  channel TEXT,
  origin_text TEXT,
  steps INTEGER NOT NULL DEFAULT 0,
  waiting TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_session ON agent_runs(session_id);
CREATE TABLE IF NOT EXISTS agent_msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  created REAL NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_calls TEXT,
  tool_call_id TEXT,
  name TEXT,
  status TEXT NOT NULL DEFAULT 'done',
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS agent_msgs_run ON agent_msgs(run_id);
CREATE TABLE IF NOT EXISTS productions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  workflow TEXT NOT NULL,
  title TEXT NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS productions_project ON productions(project_id);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  production_id TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  stage TEXT NOT NULL,
  key TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT 'null',
  deps TEXT NOT NULL DEFAULT '[]',
  round_id TEXT,
  task_id TEXT,
  note TEXT,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS artifacts_production ON artifacts(production_id, stage, key);
CREATE INDEX IF NOT EXISTS artifacts_round ON artifacts(round_id);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  created REAL NOT NULL,
  name TEXT,
  path TEXT NOT NULL,
  kind TEXT NOT NULL
);
"""

# old databases: add missing columns to tasks
MIGRATIONS = {
    "tasks": [
        ("project_id", "TEXT"),
        ("round_id", "TEXT"),
        ("idx", "INTEGER"),
        ("priority", "INTEGER"),  # 2 interactive / 1 agent / 0 background (scheduler ordering)
    ],
    "messages": [("session_id", "TEXT")],
}

JSON_COLS = (
    "params",
    "outputs",
    "metrics",
    "base_params",
    "decision",
    "assets",
    "meta",
    "source",
    "waiting",
    "tool_calls",
    "settings",
    "data",
    "deps",
)


class TransitionError(RuntimeError):
    pass


# old versions inside a production: no longer used
DEAD_ARTIFACT = ("failed", "rejected", "superseded")
DEAD_ARTIFACT_SQL = ",".join(f"'{x}'" for x in DEAD_ARTIFACT)


def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, db_path: Path, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.packs_dir = jobs_dir.parent / "packs"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_round ON tasks(round_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id)")
        self.db.commit()

    # ---- general ----
    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for c in JSON_COLS:
            if c in d and isinstance(d[c], str):
                d[c] = json.loads(d[c])
        return d

    def _rows(self, q: str, args: tuple = ()) -> list[dict]:
        return [self._row(r) for r in self.db.execute(q, args)]

    def _one(self, q: str, args: tuple = ()) -> dict | None:
        return self._row(self.db.execute(q, args).fetchone())

    @staticmethod
    def _encode(fields: dict) -> dict:
        return {
            k: json.dumps(v, ensure_ascii=False) if k in JSON_COLS else v for k, v in fields.items()
        }

    # ---- tasks: read ----
    @_locked
    def get(self, task_id: str) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    @_locked
    def list(self, limit: int = 200, project_id: str | None = None) -> list[dict]:
        if project_id:
            return self._rows(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY created DESC LIMIT ?",
                (project_id, limit),
            )
        return self._rows("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))

    @_locked
    def by_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" * len(states))
        return self._rows(f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created", states)

    @_locked
    def next_draft(self) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE state='draft' ORDER BY created, idx LIMIT 1")

    @_locked
    def find_duplicate(self, params_hash: str) -> dict | None:
        """A task with the same parameters that has not failed; failed / rejected ones may be redone (timed-out ones may still be running, so they count)."""
        return self._one(
            "SELECT * FROM tasks WHERE params_hash=? AND state NOT IN ('failed','rejected')"
            " ORDER BY created DESC LIMIT 1",
            (params_hash,),
        )

    @_locked
    def events(self, task_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
        return [dict(r) for r in rows]

    def task_dir(self, task_id: str) -> Path:
        return self.jobs_dir / task_id

    # ---- tasks: write ----
    @_locked
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
        project_id: str | None = None,
        round_id: str | None = None,
        idx: int | None = None,
        priority: int | None = None,
        state: str = "draft",
        outputs: list | None = None,
        note: str | None = None,
    ) -> dict:
        """Create a task. state / outputs are only for importing externally finished results (e.g. FreeVideo takes)."""
        now = time.time()
        task_id = _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks (id, created, updated, state, template, template_sha, kind, tier,"
                " title, parent_id, params, params_hash, project_id, round_id, idx, priority, outputs)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    now,
                    now,
                    state,
                    template,
                    template_sha,
                    kind,
                    tier,
                    title,
                    parent_id,
                    json.dumps(params, ensure_ascii=False),
                    params_hash,
                    project_id,
                    round_id,
                    idx,
                    priority,
                    json.dumps(outputs or [], ensure_ascii=False),
                ),
            )
            self.db.execute(
                "INSERT INTO events (task_id, ts, state, note) VALUES (?,?,?,?)",
                (task_id, now, state, note),
            )
        self.task_dir(task_id).mkdir(parents=True, exist_ok=True)
        self.write_manifest(task_id)
        return self.get(task_id)

    @_locked
    def update(self, task_id: str, **fields) -> None:
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE tasks SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), task_id),
            )
        self.write_manifest(task_id)

    @_locked
    def transition(self, task_id: str, new: str, note: str | None = None, **fields) -> dict:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        old = task["state"]
        if new not in TRANSITIONS.get(old, set()):
            raise TransitionError(f"{task_id}: 非法迁移 {old} → {new}")
        now = time.time()
        fields = self._encode(fields)
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

    @_locked
    def write_manifest(self, task_id: str) -> None:
        task = self.get(task_id)
        task["events"] = self.events(task_id)
        path = self.task_dir(task_id) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2))
        tmp.replace(path)

    # ---- projects ----
    @_locked
    def create_project(self, name: str, note: str | None = None) -> dict:
        now, pid = time.time(), _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO projects (id, created, updated, name, note) VALUES (?,?,?,?,?)",
                (pid, now, now, name, note),
            )
        return self.get_project(pid)

    @_locked
    def get_project(self, project_id: str) -> dict | None:
        return self._one("SELECT * FROM projects WHERE id=?", (project_id,))

    @_locked
    def find_project(self, key: str) -> dict | None:
        """Find by ID or by name (the most recent project with that name)."""
        return self.get_project(key) or self._one(
            "SELECT * FROM projects WHERE name=? ORDER BY created DESC LIMIT 1", (key,)
        )

    @_locked
    def list_projects(self) -> list[dict]:
        return self._rows("SELECT * FROM projects ORDER BY updated DESC")

    @_locked
    def update_project(self, project_id: str, **fields) -> dict:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE projects SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), project_id),
            )
        return self.get_project(project_id)

    @_locked
    def touch_project(self, project_id: str | None) -> None:
        if project_id:
            with self.db:
                self.db.execute(
                    "UPDATE projects SET updated=? WHERE id=?", (time.time(), project_id)
                )

    # ---- rounds ----
    @_locked
    def create_round(self, **fields) -> dict:
        now, rid = time.time(), _new_id()
        fields = self._encode({"id": rid, "created": now, "updated": now, **fields})
        cols = ", ".join(fields)
        with self.db:
            self.db.execute(
                f"INSERT INTO rounds ({cols}) VALUES ({','.join('?' * len(fields))})",
                tuple(fields.values()),
            )
        self.touch_project(fields.get("project_id"))
        return self.get_round(rid)

    @_locked
    def get_round(self, round_id: str) -> dict | None:
        return self._one("SELECT * FROM rounds WHERE id=?", (round_id,))

    @_locked
    def update_round(self, round_id: str, **fields) -> dict:
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE rounds SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), round_id),
            )
        r = self.get_round(round_id)
        self.touch_project(r["project_id"])
        return r

    @_locked
    def rounds(self, project_id: str | None = None, state: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM rounds WHERE 1=1", []
        if project_id:
            q += " AND project_id=?"
            args.append(project_id)
        if state:
            q += " AND state=?"
            args.append(state)
        return self._rows(q + " ORDER BY created DESC", tuple(args))

    # ---- deletion (Studio deletes the files first; here only the rows) ----
    @_locked
    def delete_tasks(self, task_ids: list[str]) -> None:
        if not task_ids:
            return
        marks = ",".join("?" * len(task_ids))
        with self.db:
            self.db.execute(f"DELETE FROM events WHERE task_id IN ({marks})", tuple(task_ids))
            self.db.execute(f"DELETE FROM tasks WHERE id IN ({marks})", tuple(task_ids))

    @_locked
    def delete_round_row(self, round_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM rounds WHERE id=?", (round_id,))

    @_locked
    def active_production_refs(self, round_ids: list[str], task_ids: list[str]) -> list[dict]:
        """Outputs an active production still uses (deleting them would leave the production missing images).

        Failed / rejected / superseded old versions inside a production are not "in use": they are only history, deleting them does not affect the production."""
        ids = [*round_ids, *task_ids]
        if not ids:
            return []
        rm, tm = ",".join("?" * len(round_ids)) or "''", ",".join("?" * len(task_ids)) or "''"
        return self._rows(
            "SELECT a.*, p.title AS production_title FROM artifacts a"
            " JOIN productions p ON p.id = a.production_id"
            f" WHERE p.state = 'active' AND a.status NOT IN ({DEAD_ARTIFACT_SQL})"
            f" AND (a.round_id IN ({rm}) OR a.task_id IN ({tm}))",
            (*round_ids, *task_ids),
        )

    @_locked
    def round_tasks(self, round_id: str) -> list[dict]:
        return self._rows("SELECT * FROM tasks WHERE round_id=? ORDER BY idx", (round_id,))

    # ---- baseline packs ----
    @_locked
    def pack(self, project_id: str, version: int | None = None) -> dict | None:
        if version is None:
            return self._one(
                "SELECT * FROM packs WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            )
        return self._one(
            "SELECT * FROM packs WHERE project_id=? AND version=?", (project_id, version)
        )

    @_locked
    def pack_history(self, project_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM packs WHERE project_id=? ORDER BY version DESC", (project_id,)
        )

    @_locked
    def add_pack_version(self, project_id: str, assets: dict, note: str | None) -> dict:
        cur = self.pack(project_id)
        version = (cur["version"] if cur else 0) + 1
        with self.db:
            self.db.execute(
                "INSERT INTO packs (project_id, version, created, assets, note) VALUES (?,?,?,?,?)",
                (project_id, version, time.time(), json.dumps(assets, ensure_ascii=False), note),
            )
        self.touch_project(project_id)
        return self.pack(project_id, version)

    # ---- chat history ----
    @_locked
    def log_message(
        self,
        channel: str,
        direction: str,
        text: str,
        project_id: str | None = None,
        author: str | None = None,
        session_id: str | None = None,
        **meta,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO messages (project_id, session_id, ts, channel, direction, author,"
                " text, meta) VALUES (?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    session_id,
                    time.time(),
                    channel,
                    direction,
                    author,
                    text,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
        if session_id:
            with self.db:
                self.db.execute(
                    "UPDATE sessions SET updated=? WHERE id=?", (time.time(), session_id)
                )
        return cur.lastrowid

    @_locked
    def session_messages(self, session_id: str, after: int = 0) -> list[dict]:
        return self._rows(
            "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id", (session_id, after)
        )

    # ---- sessions ----
    @_locked
    def create_session(self, project_id: str, title: str | None = None) -> dict:
        now, sid = time.time(), _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO sessions (id, project_id, created, updated, title) VALUES (?,?,?,?,?)",
                (sid, project_id, now, now, title),
            )
        return self.get_session(sid)

    @_locked
    def get_session(self, session_id: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    @_locked
    def sessions(self, project_id: str) -> list[dict]:
        return self._rows(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n_messages,"
            " (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.channel='telegram')"
            " AS n_telegram"
            " FROM sessions s WHERE project_id=? ORDER BY updated DESC",
            (project_id,),
        )

    @_locked
    def update_session(self, session_id: str, **fields) -> dict:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE sessions SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), session_id),
            )
        return self.get_session(session_id)

    @_locked
    def delete_session(self, session_id: str) -> dict:
        """Delete the session itself with its messages and agent run records (project memory and outputs stay)."""
        runs = [
            r["id"]
            for r in self._rows("SELECT id FROM agent_runs WHERE session_id=?", (session_id,))
        ]
        with self.db:
            n = self.db.execute("DELETE FROM messages WHERE session_id=?", (session_id,)).rowcount
            for rid in runs:
                self.db.execute("DELETE FROM agent_msgs WHERE run_id=?", (rid,))
            self.db.execute("DELETE FROM agent_runs WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.db.execute("DELETE FROM kv WHERE key=?", (f"agent_upto:{session_id}",))
        return {"messages": n, "runs": len(runs)}

    @_locked
    def adopt_orphan_messages(self, project_id: str, session_id: str) -> int:
        with self.db:
            cur = self.db.execute(
                "UPDATE messages SET session_id=? WHERE project_id=? AND session_id IS NULL",
                (session_id, project_id),
            )
        return cur.rowcount

    # ---- project memory ----
    @_locked
    def add_memory(
        self,
        project_id: str,
        text: str,
        category: str | None = None,
        status: str = "proposed",
        source: dict | None = None,
        supersedes: int | None = None,
    ) -> dict:
        now = time.time()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO memories (project_id, created, updated, text, category, status,"
                " source, supersedes) VALUES (?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    now,
                    now,
                    text,
                    category,
                    status,
                    json.dumps(source or {}, ensure_ascii=False),
                    supersedes,
                ),
            )
        return self.get_memory(cur.lastrowid)

    @_locked
    def get_memory(self, memory_id: int) -> dict | None:
        return self._one("SELECT * FROM memories WHERE id=?", (memory_id,))

    @_locked
    def memories(self, project_id: str, *statuses: str) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self._rows(
                f"SELECT * FROM memories WHERE project_id=? AND status IN ({marks})"
                " ORDER BY pinned DESC, id",
                (project_id, *statuses),
            )
        return self._rows(
            "SELECT * FROM memories WHERE project_id=? ORDER BY id DESC", (project_id,)
        )

    @_locked
    def update_memory(self, memory_id: int, **fields) -> dict:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE memories SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), memory_id),
            )
        return self.get_memory(memory_id)

    # ---- agent runs (A1): every message stored verbatim (prefix cache and crash recovery depend on
    # it) ----
    @_locked
    def create_run(
        self, project_id: str | None, session_id: str | None, channel: str, origin_text: str
    ) -> dict:
        now, rid = time.time(), _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO agent_runs (id, project_id, session_id, created, updated, status,"
                " channel, origin_text) VALUES (?,?,?,?,?,?,?,?)",
                (rid, project_id, session_id, now, now, "running", channel, origin_text),
            )
        return self.get_run(rid)

    @_locked
    def get_run(self, run_id: str) -> dict | None:
        return self._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))

    @_locked
    def update_run(self, run_id: str, **fields) -> dict:
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE agent_runs SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), run_id),
            )
        return self.get_run(run_id)

    @_locked
    def runs_of_project(self, project_id: str) -> list[dict]:
        return self._rows("SELECT * FROM agent_runs WHERE project_id=?", (project_id,))

    @_locked
    def runs(self, session_id: str | None = None, *statuses: str) -> list[dict]:
        q, args = "SELECT * FROM agent_runs WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return self._rows(q + " ORDER BY created", tuple(args))

    @_locked
    def add_agent_msg(
        self,
        run_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        status: str = "done",
        **meta,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO agent_msgs (run_id, created, role, content, tool_calls, tool_call_id,"
                " name, status, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    time.time(),
                    role,
                    content,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
                    tool_call_id,
                    name,
                    status,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
        return cur.lastrowid

    @_locked
    def agent_msgs(self, run_id: str) -> list[dict]:
        return self._rows("SELECT * FROM agent_msgs WHERE run_id=? ORDER BY id", (run_id,))

    @_locked
    def get_agent_msg(self, msg_id: int) -> dict | None:
        return self._one("SELECT * FROM agent_msgs WHERE id=?", (msg_id,))

    @_locked
    def update_agent_msg(self, msg_id: int, **fields) -> None:
        if "meta" in fields:
            cur = self.get_agent_msg(msg_id)["meta"]
            fields["meta"] = {**cur, **fields["meta"]}
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE agent_msgs SET {cols} WHERE id=?", (*fields.values(), msg_id))

    @_locked
    def pending_tool_msgs(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM agent_msgs WHERE role='tool' AND status != 'done'"
        args: tuple = ()
        if status:
            q += " AND status=?"
            args = (status,)
        return self._rows(q + " ORDER BY id", args)

    # ---- uploads (reference images) ----
    @_locked
    def add_upload(self, project_id: str | None, name: str, path: str, kind: str) -> dict:
        uid = uuid.uuid4().hex[:10]
        with self.db:
            self.db.execute(
                "INSERT INTO uploads (id, project_id, created, name, path, kind) VALUES (?,?,?,?,?,?)",
                (uid, project_id, time.time(), name, path, kind),
            )
        return self.get_upload(uid)

    @_locked
    def get_upload(self, upload_id: str) -> dict | None:
        return self._one("SELECT * FROM uploads WHERE id=?", (upload_id,))

    @_locked
    def messages(self, project_id: str | None = None, limit: int = 500) -> list[dict]:
        if project_id:
            return self._rows(
                "SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            )[::-1]
        return self._rows("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))[::-1]

    # ---- key-value settings (current project, residency mode, pause, pending notes, ...) ----
    @_locked
    def get_kv(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    @_locked
    def set_kv(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---- productions: productions and their artifacts ----
    @_locked
    def create_production(self, project_id: str, workflow: str, title: str, settings: dict) -> dict:
        now, pid = time.time(), _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO productions (id, project_id, created, updated, workflow, title, settings)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    pid,
                    project_id,
                    now,
                    now,
                    workflow,
                    title,
                    json.dumps(settings, ensure_ascii=False),
                ),
            )
        return self.get_production(pid)

    @_locked
    def get_production(self, production_id: str) -> dict | None:
        return self._one("SELECT * FROM productions WHERE id=?", (production_id,))

    @_locked
    def productions(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            return self._rows(
                "SELECT * FROM productions WHERE project_id=? ORDER BY created DESC", (project_id,)
            )
        return self._rows("SELECT * FROM productions ORDER BY created DESC")

    @_locked
    def update_production(self, production_id: str, **fields) -> dict:
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE productions SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), production_id),
            )
        return self.get_production(production_id)

    @_locked
    def production_rounds(self, production_id: str) -> list[dict]:
        """Render rounds the production itself created (not outputs you picked into it)."""
        return self._rows(
            "SELECT * FROM rounds WHERE request LIKE ?", (f"production:{production_id}:%",)
        )

    @_locked
    def delete_production_rows(self, production_id: str) -> int:
        """Delete a production and its artifacts (the engine removes rounds and files first)."""
        with self.db:
            n = self.db.execute(
                "DELETE FROM artifacts WHERE production_id=?", (production_id,)
            ).rowcount
            self.db.execute("DELETE FROM productions WHERE id=?", (production_id,))
        return n

    @_locked
    def delete_project_rows(self, project_id: str) -> dict:
        """Delete a project's own rows: sessions with messages and agent runs, memories, packs,
        upload records, its kv keys and the project. Tasks, rounds and productions go first
        (with their files), through Studio and the production engine."""
        sessions = [
            r["id"] for r in self._rows("SELECT id FROM sessions WHERE project_id=?", (project_id,))
        ]
        runs = [
            r["id"]
            for r in self._rows("SELECT id FROM agent_runs WHERE project_id=?", (project_id,))
        ]
        with self.db:
            n = self.db.execute("DELETE FROM messages WHERE project_id=?", (project_id,)).rowcount
            for rid in runs:
                self.db.execute("DELETE FROM agent_msgs WHERE run_id=?", (rid,))
            self.db.execute("DELETE FROM agent_runs WHERE project_id=?", (project_id,))
            for sid in sessions:
                self.db.execute("DELETE FROM kv WHERE key=?", (f"agent_upto:{sid}",))
            self.db.execute("DELETE FROM sessions WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM memories WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM packs WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM uploads WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM kv WHERE key=?", (f"session:{project_id}",))
            self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return {"messages": n, "sessions": len(sessions), "runs": len(runs)}

    @_locked
    def add_artifact(self, production_id: str, stage: str, key: str, status: str, **fields) -> dict:
        now, aid = time.time(), _new_id()
        row = self.db.execute(
            "SELECT MAX(version) FROM artifacts WHERE production_id=? AND stage=? AND key=?",
            (production_id, stage, key),
        ).fetchone()
        version = (row[0] or 0) + 1
        base = {"data": None, "deps": [], "meta": {}, **fields}
        enc = self._encode(base)
        cols = ", ".join(enc)
        with self.db:
            self.db.execute(
                "INSERT INTO artifacts (id, production_id, created, updated, stage, key, version,"
                f" status, {cols}) VALUES (?,?,?,?,?,?,?,?,{','.join('?' * len(enc))})",
                (aid, production_id, now, now, stage, key, version, status, *enc.values()),
            )
        return self.get_artifact(aid)

    @_locked
    def get_artifact(self, artifact_id: str) -> dict | None:
        return self._one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))

    @_locked
    def artifact_by_round(self, round_id: str) -> dict | None:
        return self._one("SELECT * FROM artifacts WHERE round_id=?", (round_id,))

    @_locked
    def artifacts(self, production_id: str, stage: str | None = None, *statuses: str) -> list[dict]:
        q, args = "SELECT * FROM artifacts WHERE production_id=?", [production_id]
        if stage:
            q += " AND stage=?"
            args.append(stage)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return self._rows(q + " ORDER BY stage, key, version", tuple(args))

    @_locked
    def update_artifact(self, artifact_id: str, **fields) -> dict:
        fields = self._encode(fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(
                f"UPDATE artifacts SET {cols}, updated=? WHERE id=?",
                (*fields.values(), time.time(), artifact_id),
            )
        return self.get_artifact(artifact_id)
