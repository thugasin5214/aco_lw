"""Local services you start / stop by hand from the console: ComfyUI, LM Studio, other servers.

They are defined in `data/services.yaml` (machine-specific commands and paths, not in the repo;
`services.example.yaml` shows the format). This is a manual control panel: the agent has no tool
for it. aco only stops what it started itself (its own pid file) or runs the stop command you
configured — it never kills a process it did not start. A service can declare `protect: renders`:
then it cannot be stopped while aco has a render submitted or running.
"""

from __future__ import annotations

import glob
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import yaml

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
FIELDS = {
    "id",
    "name",
    "start",
    "stop",
    "cwd",
    "env",
    "health",
    "port",
    "log",
    "note",
    "protect",
}


class ServiceError(ValueError):
    pass


def _expand(p: str | None) -> str | None:
    return os.path.expanduser(os.path.expandvars(p)) if p else p


def validate(rows: list) -> list[dict]:
    """Check the list from services.yaml / the console; returns it normalised."""
    if not isinstance(rows, list):
        raise ServiceError("services.yaml must be a list of services")
    seen, out = set(), []
    for r in rows:
        if not isinstance(r, dict):
            raise ServiceError("each service must be a mapping")
        extra = set(r) - FIELDS
        if extra:
            raise ServiceError(f"unknown field(s) {sorted(extra)} in service {r.get('id')}")
        sid = str(r.get("id") or "")
        if not ID.match(sid):
            raise ServiceError(f"bad id {sid!r}: lowercase letters, digits, - and _")
        if sid in seen or sid == "aco":
            raise ServiceError(f"duplicate or reserved id {sid!r}")
        seen.add(sid)
        if not (r.get("start") or r.get("health") or r.get("port")):
            raise ServiceError(f"{sid}: needs a start command or a health check")
        if r.get("port") is not None and not (1 <= int(r["port"]) <= 65535):
            raise ServiceError(f"{sid}: bad port")
        if r.get("env") is not None and not isinstance(r["env"], dict):
            raise ServiceError(f"{sid}: env must be a mapping")
        if r.get("protect") not in (None, "renders"):
            raise ServiceError(f"{sid}: protect can only be 'renders'")
        out.append({k: v for k, v in r.items() if v not in (None, "")})
    return out


class Services:
    def __init__(self, data_dir: Path, renders_busy: Callable[[], int] = lambda: 0):
        self.path = data_dir / "services.yaml"
        self.run_dir = data_dir / "services"
        self.renders_busy = renders_busy  # number of aco renders submitted / running
        self._procs: dict[str, subprocess.Popen] = {}  # started by this process: reaped here

    # ---- definitions ----
    def specs(self) -> list[dict]:
        if not self.path.exists():
            return []
        return validate(yaml.safe_load(self.path.read_text()) or [])

    def spec(self, sid: str) -> dict:
        s = next((x for x in self.specs() if x["id"] == sid), None)
        if s is None:
            raise ServiceError(f"no service {sid}")
        return s

    def raw(self) -> str:
        return self.path.read_text() if self.path.exists() else ""

    def save_raw(self, text: str) -> list[dict]:
        rows = validate(yaml.safe_load(text) or [])
        self.path.write_text(text if text.endswith("\n") else text + "\n")
        return rows

    def upsert(self, row: dict) -> list[dict]:
        rows = [r for r in self.specs() if r["id"] != row.get("id")]
        rows = validate([*rows, row])
        self.path.write_text(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False))
        return rows

    def remove(self, sid: str) -> list[dict]:
        rows = [r for r in self.specs() if r["id"] != sid]
        self.path.write_text(yaml.safe_dump(rows, allow_unicode=True, sort_keys=False))
        return rows

    # ---- state ----
    def _pidfile(self, sid: str) -> Path:
        return self.run_dir / f"{sid}.pid"

    def _logfile(self, s: dict) -> Path:
        return self.run_dir / f"{s['id']}.log"

    @staticmethod
    def _started(pid: int) -> str:
        """The process start time: with the pid it identifies our process even after the pid
        is reused."""
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
        )
        return r.stdout.strip()

    def own_pid(self, sid: str) -> int | None:
        """The pid aco started for this service, if that process is still alive."""
        p = self._procs.get(sid)
        if p is not None and p.poll() is not None:  # exited: reap it (no zombie)
            self._procs.pop(sid, None)
            self._pidfile(sid).unlink(missing_ok=True)
            return None
        f = self._pidfile(sid)
        try:
            pid_s, _, started = f.read_text().strip().partition(" ")
            pid = int(pid_s)
            os.kill(pid, 0)
        except (OSError, ValueError):
            return None
        if started and self._started(pid) != started:
            return None  # the pid now belongs to another process
        return pid

    @staticmethod
    def healthy(s: dict, timeout: float = 1.5) -> bool | None:
        """None: no health check configured."""
        if s.get("health"):
            try:
                with urllib.request.urlopen(s["health"], timeout=timeout) as r:
                    return r.status < 500
            except Exception:  # noqa: BLE001 — down / refused / timeout
                return False
        if s.get("port"):
            with socket.socket() as sock:
                sock.settimeout(timeout)
                return sock.connect_ex(("127.0.0.1", int(s["port"]))) == 0
        return None

    def status(self, s: dict) -> dict:
        pid = self.own_pid(s["id"])
        up = self.healthy(s)
        return {
            **s,
            "up": up if up is not None else bool(pid),
            "pid": pid,
            "ours": pid is not None,
            "can_stop": bool(s.get("stop") or pid),
        }

    def list(self) -> list[dict]:
        return [self.status(s) for s in self.specs()]

    # ---- actions ----
    def start(self, sid: str) -> dict:
        s = self.spec(sid)
        if not s.get("start"):
            raise ServiceError(f"{sid}: no start command configured")
        st = self.status(s)
        if st["up"] or st["pid"]:
            raise ServiceError(f"{s.get('name', sid)} is already running")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self._logfile(s).open("ab")
        log.write(f"\n==== start {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
        log.flush()
        cwd = _expand(s.get("cwd"))
        if cwd and not Path(cwd).is_dir():
            raise ServiceError(f"{sid}: cwd {cwd} does not exist")
        env = {**os.environ, **{k: _expand(str(v)) for k, v in (s.get("env") or {}).items()}}
        p = subprocess.Popen(
            s["start"],
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own process group: stop ends its children too
        )
        self._procs[sid] = p
        self._pidfile(sid).write_text(f"{p.pid} {self._started(p.pid)}\n")
        return {"id": sid, "pid": p.pid, "started": True}

    def stop(self, sid: str, wait_s: float = 10.0) -> dict:
        s = self.spec(sid)
        if s.get("protect") == "renders" and (n := self.renders_busy()):
            raise ServiceError(
                f"{s.get('name', sid)}: aco has {n} render(s) running; stop or wait for them first"
            )
        pid = self.own_pid(sid)
        if s.get("stop"):
            r = subprocess.run(
                s["stop"],
                shell=True,
                check=False,
                cwd=_expand(s.get("cwd")),
                capture_output=True,
                text=True,
                timeout=60,
            )
            how = f"stop command (exit {r.returncode})"
        elif pid:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            how = "SIGTERM to the process group aco started"
        else:
            raise ServiceError(
                f"{s.get('name', sid)} was not started by aco and has no stop command; "
                "stop it where you started it"
            )
        deadline = time.monotonic() + wait_s
        while pid and self.own_pid(sid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if pid and not self.own_pid(sid):
            self._pidfile(sid).unlink(missing_ok=True)
        return {"id": sid, "stopped": True, "how": how}

    def restart(self, sid: str) -> dict:
        st = self.status(self.spec(sid))
        if st["up"] or st["pid"]:
            self.stop(sid)
            deadline = time.monotonic() + 15
            while self.healthy(st) and time.monotonic() < deadline:
                time.sleep(0.5)
        return self.start(sid)

    def log(self, sid: str, lines: int = 200) -> dict:
        """The last lines of its log: the file you configured (a glob picks the newest match)
        or the output aco captured when it started it."""
        s = self.spec(sid)
        path = None
        if s.get("log"):
            hits = sorted(glob.glob(_expand(s["log"])), key=os.path.getmtime)
            path = Path(hits[-1]) if hits else None
        if path is None and self._logfile(s).exists():
            path = self._logfile(s)
        if path is None:
            return {"id": sid, "path": None, "text": ""}
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            text = f.read().decode("utf-8", "replace")
        return {"id": sid, "path": str(path), "text": "\n".join(text.splitlines()[-lines:])}
