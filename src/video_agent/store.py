"""持久化：SQLite 做索引，每任务目录下的 manifest.json 是可读副本。

层级：项目 project → 候选轮 round → 任务 task；基准包 pack 与对话 messages 挂在项目下。
所有任务状态变化都经 `transition()`，非法迁移直接抛错；每次变化写 events 并重写 manifest。
同一连接会被 daemon 的多个线程使用，所有访问经一把可重入锁串行化。
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
TERMINAL = {"done", "rejected", "failed", "timeout"}  # done / rejected 可经 reopen 回到待审阅
ACTIVE = {"submitted", "running"}
TRANSITIONS = {
    "draft": {"validated", "failed"},
    "validated": {"waiting_memory", "failed"},
    "waiting_memory": {"submitted", "failed", "timeout"},
    "submitted": {"running", "review", "failed", "timeout"},
    "running": {"review", "failed", "timeout"},
    "review": {"done", "rejected"},
    "done": {"review"},  # 审阅结果可更改：重新打开
    "rejected": {"review"},
    "timeout": {"review", "failed"},  # 超时只是停止观察；恢复时可能发现已完成
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
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  created REAL NOT NULL,
  name TEXT,
  path TEXT NOT NULL,
  kind TEXT NOT NULL
);
"""

# 旧库升级：给 tasks 补列
MIGRATIONS = {
    "tasks": [
        ("project_id", "TEXT"),
        ("round_id", "TEXT"),
        ("idx", "INTEGER"),
    ],
    "messages": [("session_id", "TEXT")],
}

JSON_COLS = ("params", "outputs", "metrics", "base_params", "decision", "assets", "meta", "source")


class TransitionError(RuntimeError):
    pass


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

    # ---- 通用 ----
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

    # ---- 任务：读 ----
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
        """同参数且未失败的任务；失败/打回的可以重做（超时的可能仍在跑，不算）。"""
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

    # ---- 任务：写 ----
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
    ) -> dict:
        now = time.time()
        task_id = _new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks (id, created, updated, state, template, template_sha, kind, tier,"
                " title, parent_id, params, params_hash, project_id, round_id, idx)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    project_id,
                    round_id,
                    idx,
                ),
            )
            self.db.execute(
                "INSERT INTO events (task_id, ts, state, note) VALUES (?,?,?,?)",
                (task_id, now, "draft", None),
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

    # ---- 项目 ----
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
        """按 ID 或名称（最近的同名项目）查找。"""
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

    # ---- 候选轮 ----
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

    @_locked
    def round_tasks(self, round_id: str) -> list[dict]:
        return self._rows("SELECT * FROM tasks WHERE round_id=? ORDER BY idx", (round_id,))

    # ---- 基准包 ----
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

    # ---- 对话历史 ----
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

    # ---- 会话 ----
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
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n_messages"
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
    def adopt_orphan_messages(self, project_id: str, session_id: str) -> int:
        with self.db:
            cur = self.db.execute(
                "UPDATE messages SET session_id=? WHERE project_id=? AND session_id IS NULL",
                (session_id, project_id),
            )
        return cur.rowcount

    # ---- 项目记忆 ----
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

    # ---- 上传（参考图） ----
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

    # ---- 键值设置（当前项目、驻留模式、暂停、待补批注等） ----
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
