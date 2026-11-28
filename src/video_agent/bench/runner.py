"""Run one case against the live daemon, playing the user.

The runner talks to aco only through its console API (the same calls a person makes) and reads
the database to see what is going on. It records its own timeline (events.jsonl in the run folder):
what it sent and when, which is what steering latency and compliance are measured against.

Online brains need `allow_online`; it is switched on for the run and restored afterwards, as are
the brain and the permission mode (bypass: the scripted user would approve anyway; installs and
downloads still ask, and the scripted user declines them).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from ..config import load as load_config
from ..store import Store
from .cases import Case

log = logging.getLogger(__name__)

TERMINAL = {"done", "failed", "cancelled"}
POLL_S = 5.0
SETTLE_S = 60.0  # nothing left to do for this long after the last run ended → finished


class Runner:
    def __init__(self, case: Case, brain: str = "local", base_url: str | None = None):
        self.cfg = load_config()
        self.case, self.brain = case, brain
        self.url = base_url or f"http://127.0.0.1:{self.cfg.web_port}"
        self.api = httpx.Client(base_url=self.url, timeout=60)
        self.store = Store(self.cfg.db_path, self.cfg.jobs_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{case.id}-{brain}"
        self.dir = self.cfg.data_dir / "bench" / "runs" / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = 0.0
        self.fired: set[int] = set()
        self.answered: set[int] = set()

    # ---- timeline ----
    def event(self, kind: str, **data) -> None:
        row = {"t": round(time.time() - self.t0, 1), "ts": time.time(), "kind": kind, **data}
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def post(self, path: str, body: dict) -> dict:
        r = self.api.post(path, json=body)
        if r.status_code >= 400:
            self.event("api_error", path=path, status=r.status_code, body=r.text[:300])
            return {}
        return r.json()

    def say(self, text: str, why: str) -> None:
        self.event("user", text=text, why=why)
        self.post("/api/chat", {"text": text})

    # ---- setup / teardown ----
    def _setup(self) -> dict:
        got = self.api.get("/api/config").json()
        before = {
            "allow_online": next(
                (f.get("value") for f in got["fields"] if f["name"] == "allow_online"), False
            ),
            "runtime": got["runtime"],
        }
        project = self.post("/api/projects", {"name": f"bench · {self.case.id} · {self.brain}"})
        self.project_id = project["id"]
        self.post(f"/api/projects/{self.project_id}/use", {})
        self.post(
            "/api/config",
            {
                "values": {"allow_online": self.brain != "local"},
                "runtime": {"agent_brain": self.brain, "permission_mode": "bypass"},
            },
        )
        (self.dir / "run.json").write_text(
            json.dumps(
                {
                    "case": self.case.model_dump(),
                    "brain": self.brain,
                    "project_id": self.project_id,
                    "started": time.time(),
                    "aco_url": self.url,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return before

    def _restore(self, before: dict) -> None:
        runtime = before.get("runtime") or {}
        self.post(
            "/api/config",
            {
                "values": {"allow_online": bool(before.get("allow_online"))},
                "runtime": {
                    k: runtime[k] for k in ("agent_brain", "permission_mode") if runtime.get(k)
                },
            },
        )

    # ---- what is going on ----
    def runs(self) -> list[dict]:
        return sorted(self.store.runs_of_project(self.project_id), key=lambda r: r["created"])

    def tasks(self) -> list[dict]:
        return [
            t for r in self.store.rounds(self.project_id) for t in self.store.round_tasks(r["id"])
        ]

    def _milestones(self) -> set[str]:
        done = set()
        for t in self.tasks():
            if t["outputs"]:
                done.add("first_round")
                done.add("first_video" if t["kind"] == "video" else "first_image")
        runs = self.runs()
        if runs and all(r["status"] in TERMINAL for r in runs):
            done.add("run_done")
        for p in self.store.productions(self.project_id):
            for a in self.store.artifacts(p["id"]):
                if a["status"] in ("current", "review"):
                    done.add(f"stage:{a['stage']}")
        return done

    # ---- the scripted user ----
    def _answer_pending(self) -> None:
        for m in self.store.pending_tool_msgs():
            run = self.store.get_run(m["run_id"])
            if run is None or run["project_id"] != self.project_id or m["id"] in self.answered:
                continue
            kind = m["meta"].get("kind")
            if kind == "approval":
                self.answered.add(m["id"])
                yes = self.case.user.approvals == "approve" and m["meta"].get("bypassable", True)
                self.event("approval", msg=m["id"], approved=yes, tool=m["name"])
                self.post("/api/action", {"data": f"a:{m['id']}:{'yes' if yes else 'no'}"})
            elif kind == "user":
                self.answered.add(m["id"])
                self.say(self.case.user.answer, f"answer to {m['name']}")
        # production stages waiting for review: approve (the scripted user accepts the plan)
        for p in self.store.productions(self.project_id):
            if p["state"] != "active":
                continue
            for a in self.store.artifacts(p["id"], None, "review", "invalid"):
                if a["round_id"] or a["id"] in self.answered:
                    continue
                self.answered.add(a["id"])
                action = "approve" if a["status"] == "review" else "redo"
                self.event("review", stage=a["stage"], key=a["key"], action=action)
                self.post(
                    f"/api/productions/{p['id']}/review",
                    {"stage": a["stage"], "action": action, "key": a["key"]},
                )
        # rounds waiting for a pick that no agent is handling (production renders)
        busy = any(r["status"] not in TERMINAL for r in self.runs())
        if not busy:
            for r in self.store.rounds(self.project_id, "review"):
                if r["id"] in self.answered or not self.store.artifact_by_round(r["id"]):
                    continue
                ok = [t for t in self.store.round_tasks(r["id"]) if t["outputs"]]
                if ok:
                    self.answered.add(r["id"])
                    idx = ok[min(self.case.user.pick, len(ok)) - 1]["idx"]
                    self.event("pick", round=r["id"], idx=idx)
                    self.post(f"/api/rounds/{r['id']}/select", {"idx": idx})

    def _steer(self) -> None:
        reached = self._milestones()
        elapsed = time.time() - self.t0
        for i, s in enumerate(self.case.user.steer):
            if i in self.fired:
                continue
            due = (
                elapsed >= float(s.after.split(":", 1)[1])
                if s.after.startswith("seconds:")
                else s.after in reached
            )
            if due:
                self.fired.add(i)
                self.say(s.say, f"steer {i} ({s.after})")

    def _busy(self) -> bool:
        if any(r["status"] not in TERMINAL for r in self.runs()):
            return True
        if any(
            t["state"] in ("draft", "validated", "waiting_memory", "submitted", "running")
            for t in self.tasks()
        ):
            return True
        for p in self.store.productions(self.project_id):
            if p["state"] == "active" and any(
                a["status"] in ("queued", "running") for a in self.store.artifacts(p["id"])
            ):
                return True
        return False

    # ---- run ----
    def run(self) -> Path:
        before = self._setup()
        self.t0 = time.time()
        self.event("start", brain=self.brain, case=self.case.id)
        limit = self.case.limits.hours * 3600
        idle_since: float | None = None
        try:
            self.say(self.case.brief.strip() + self.case.delivery_note(), "brief")
            while True:
                time.sleep(POLL_S)
                self._answer_pending()
                self._steer()
                if time.time() - self.t0 > limit:
                    self.event("limit", hours=self.case.limits.hours)
                    for r in self.runs():
                        if r["status"] not in TERMINAL:
                            self.post(f"/api/runs/{r['id']}/cancel", {})
                    break
                pending_steer = len(self.fired) < len(self.case.user.steer)
                if self._busy() or (pending_steer and not self._all_steer_unreachable()):
                    idle_since = None
                    continue
                idle_since = idle_since or time.time()
                if time.time() - idle_since > SETTLE_S:
                    break
        finally:
            self.event("end")
            self._restore(before)
        info = json.loads((self.dir / "run.json").read_text())
        info["ended"] = time.time()
        info["wall_s"] = round(info["ended"] - self.t0, 1)
        (self.dir / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        return self.dir

    def _all_steer_unreachable(self) -> bool:
        """Remaining steers wait for milestones that can no longer come (the work is over)."""
        reached = self._milestones()
        for i, s in enumerate(self.case.user.steer):
            if i in self.fired:
                continue
            if s.after.startswith("seconds:") or s.after == "run_done" or s.after in reached:
                return False
        return True
