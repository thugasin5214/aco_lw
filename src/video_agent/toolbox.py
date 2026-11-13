"""Generic tool layer: the agent is the brain and only proposes tool calls; the orchestrator (here) executes them, stores the result and hands it back when it can.

- Slow tools (reading web pages, running commands) go to the background tool queue (ToolJobs); they do not hold the agent thread or need the LLM online:
  when done the result is written into the matching tool message; if the LLM is unloaded at that moment (a video render needs the memory), the run waits in waiting_resources
  and gets the result once memory frees up (exactly the same path as waiting for a render).
- Commands always need your approval first (Telegram / console buttons), unless the command starts with a prefix listed in
  `run_command.auto_allow` in policies.yaml (empty by default). Still refused after approval: direct access to ComfyUI / FreeVideo,
  reading the FreeVideo token — generation side effects only go through the orchestrator's generation tools (resource gate, only our own tasks).
- Files: reads limited to data/ and skill directories; writes limited to this run's work directory data/agent-work/<run>/.
- Skills: compatible with Claude Code's SKILL.md (built-in skills/, data/skills/, `agent_skill_dirs`, which by default includes
  ~/.claude/skills). The system prompt lists only names and descriptions; the agent load_skill's the full text when needed and follows it with tools.
- Memory: the agent's own long-term memory in data/agent_memory/*.md (MEMORY.md is the index and goes into the system prompt).
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("aco_lw.toolbox")

# characters of output tail handed back to the LLM; the full text stays in the log file
OUT_TAIL = 6000
MAX_TIMEOUT_S = 3600
MAX_READ_CHARS = 20000
# never executed even when approved: generation side effects only go through the orchestrator; the
# token is read only inside the orchestrator process
BLOCKED = [
    (
        re.compile(r"(127\.0\.0\.1|localhost|0\.0\.0\.0)?:8188\b"),
        "ComfyUI / FreeVideo 只能经编排器的生成工具使用",
    ),
    (re.compile(r"agent/token"), "FreeVideo agent token 不能被读取"),
    (re.compile(r"\b(pkill|killall)\b|\bkill\s+-?\w*\s*\d"), "不结束进程（可能是别人的任务）"),
    (re.compile(r"\blms\s+(unload|load)\b"), "LLM 的加载 / 卸载由编排器按内存决定"),
]


def blocked(command: str) -> str | None:
    for pat, why in BLOCKED:
        if pat.search(command):
            return why
    return None


def _tail(text: str, n: int = OUT_TAIL) -> str:
    return text if len(text) <= n else "…(earlier output cut)\n" + text[-n:]


class ToolJobs:
    """Background tool queue: slow tools run here and call done(key, result) when finished."""

    def __init__(self, done: Callable[[str, dict], object], workers: int = 2):
        self.done = done
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool-job")
        self.running: dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self, key: str, fn: Callable[[], dict]) -> None:
        with self._lock:
            self.running[key] = time.time()

        def run() -> None:
            try:
                result = fn()
            except Exception as e:
                log.warning("tool job %s failed", key, exc_info=True)
                result = {"error": f"{type(e).__name__}: {e}"}
            with self._lock:
                self.running.pop(key, None)
            self.done(key, result)

        self.pool.submit(run)

    def busy(self, key: str) -> bool:
        with self._lock:
            return key in self.running


class Toolbox:
    def __init__(self, cfg, runtime):
        self.cfg, self.rt = cfg, runtime
        self.work_root = cfg.data_dir / "agent-work"
        self.memory_dir = cfg.data_dir / "agent_memory"
        self.jobs = ToolJobs(runtime.resolve, getattr(cfg, "tool_workers", 2))
        from .mcp_hub import MCPHub

        self.mcp = MCPHub(cfg)

    # ---- paths ----
    def workdir(self, run_id: str) -> Path:
        d = self.work_root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def skill_roots(self) -> list[Path]:
        roots = [self.cfg.skills_dir, self.cfg.data_dir / "skills"]
        roots += [Path(p).expanduser() for p in getattr(self.cfg, "agent_skill_dirs", [])]
        return [r for r in roots if r.is_dir()]

    def _resolve(self, run_id: str, path: str) -> Path:
        path = path.strip()
        store = self.rt.studio.store
        if path.startswith("@task:"):  # task output: the first output file
            task = store.get(path[len("@task:") :])
            if not task or not task["outputs"]:
                raise ValueError(f"{path}: no such task or it has no output")
            return (store.task_dir(task["id"]) / task["outputs"][0]["file"]).resolve()
        if path.startswith("@upload:"):
            up = store.get_upload(path[len("@upload:") :])
            if not up:
                raise ValueError(f"{path}: no such upload")
            return Path(up["path"]).resolve()
        p = Path(path).expanduser()
        return (p if p.is_absolute() else self.workdir(run_id) / p).resolve()

    def readable(self, run_id: str, path: str) -> Path:
        p = self._resolve(run_id, path)
        roots = [self.cfg.data_dir.resolve(), *(r.resolve() for r in self.skill_roots())]
        if not any(p.is_relative_to(r) for r in roots):
            raise ValueError(f"只能读 data/ 与 skill 目录下的文件：{p}")
        if "agent/token" in str(p):
            raise ValueError("FreeVideo agent token 不能被读取")
        return p

    def writable(self, run_id: str, path: str) -> Path:
        p = self._resolve(run_id, path)
        if not p.is_relative_to(self.work_root.resolve()):
            raise ValueError(f"只能写本次运行的工作目录 {self.workdir(run_id)}：{p}")
        return p

    # ---- commands ----
    def auto_allowed(self, command: str) -> bool:
        prefixes = self.rt.policies.tool("run_command").get("auto_allow") or []
        c = command.strip()
        # pipes / chaining / redirects / subshells are never auto-allowed: a prefix only means
        # something for a single command
        if re.search(r"[;&|`$<>]|\n", c):
            return False
        return any(c == p or c.startswith(p + " ") for p in prefixes)

    def run_command(self, run_id: str, command: str, timeout_s: int) -> dict:
        d = self.workdir(run_id)
        logf = d / f"cmd-{int(time.time() * 1000)}.log"
        before = {p.name for p in d.iterdir()}
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                ["/bin/zsh", "-lc", command],
                check=False,
                cwd=d,
                capture_output=True,
                text=True,
                encoding="utf-8",
                # output may be cut inside a multi-byte character (e.g. head -c); that must not fail
                # the whole command
                errors="replace",
                timeout=min(max(timeout_s, 1), MAX_TIMEOUT_S),
                stdin=subprocess.DEVNULL,
            )
            code, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            code = None
            out = (
                (e.stdout or b"").decode("utf-8", "replace")
                if isinstance(e.stdout, bytes)
                else (e.stdout or "")
            )
            err = (
                (e.stderr or b"").decode("utf-8", "replace")
                if isinstance(e.stderr, bytes)
                else (e.stderr or "")
            )
            err += f"\n(timed out after {timeout_s}s)"
        logf.write_text(f"$ {command}\n--- stdout\n{out}\n--- stderr\n{err}\n")
        new = sorted(p.name for p in d.iterdir() if p.name not in before and p != logf)
        return {
            "exit_code": code,
            "seconds": round(time.monotonic() - t0, 1),
            "stdout": _tail(out),
            "stderr": _tail(err, 2000),
            "workdir": str(d),
            "new_files": new[:50],
            "log": str(logf),
        }

    # ---- web pages ----
    def web_fetch(self, run_id: str, url: str, max_chars: int) -> dict:
        import httpx
        import trafilatura

        from .ingest import MAX_PAGE_BYTES, UA

        client = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": UA})
        with client as c, c.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            raw = b""
            for chunk in r.iter_bytes():
                raw += chunk
                if len(raw) > MAX_PAGE_BYTES:
                    raise ValueError("page larger than 5 MB")
            final = str(r.url)
            enc = r.encoding or "utf-8"
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            raise ValueError(
                f"not a text page ({ctype or 'unknown type'}); use run_command to download files"
            )
        body = raw.decode(enc, errors="replace")
        text, title = body, ""
        if "html" in ctype:
            text = trafilatura.extract(body, include_comments=False, include_links=True) or ""
            meta = trafilatura.extract_metadata(body)
            title = (meta.title if meta else None) or ""
        saved = self.workdir(run_id) / f"page-{int(time.time() * 1000)}.txt"
        saved.write_text(text)
        cap = min(max_chars or MAX_READ_CHARS, MAX_READ_CHARS)
        return {
            "url": final,
            "title": title,
            "chars": len(text),
            "text": text[:cap] + ("…(truncated; full text in saved)" if len(text) > cap else ""),
            "saved": str(saved),
            "note": ""
            if text.strip()
            else "no readable text (page may need a logged-in browser: "
            "try a skill such as agent-reach via run_command)",
        }

    # ---- files ----
    def read_file(self, run_id: str, path: str, offset: int, limit: int) -> dict:
        p = self.readable(run_id, path)
        if p.is_dir():
            items = sorted(p.iterdir())[:200]
            return {"path": str(p), "dir": [x.name + ("/" if x.is_dir() else "") for x in items]}
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"}:
            return {
                "path": str(p),
                "bytes": p.stat().st_size,
                "note": "binary media: use send_file to show it to the user, add_media to use it "
                "in generation, or run_command (ffprobe) to inspect it",
            }
        lines = p.read_text(errors="replace").splitlines()
        start = max(offset, 0)
        chunk = "\n".join(lines[start : start + (limit or 400)])
        return {
            "path": str(p),
            "lines": len(lines),
            "from": start,
            "text": chunk[:MAX_READ_CHARS],
        }

    def write_file(self, run_id: str, path: str, content: str) -> dict:
        p = self.writable(run_id, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"path": str(p), "bytes": len(content.encode())}

    # ---- skill ----
    def library(self):
        from .skills import SkillLibrary

        roots = [(self.cfg.skills_dir, "builtin"), (self.cfg.data_dir / "skills", "user")]
        roots += [
            (Path(p).expanduser(), "external") for p in getattr(self.cfg, "agent_skill_dirs", [])
        ]
        return SkillLibrary(*roots)

    def skills_index(self) -> str:
        rows = [
            f"- {s['name']}: {s['description'][:300]}"
            for s in self.library().list()
            if s["description"]
        ]
        return "\n".join(rows)

    def load_skill(self, name: str) -> dict:
        s = self.library().get(name)
        files = sorted(
            str(p.relative_to(s.dir))
            for p in s.dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )[:100]
        return {
            "name": s.name,
            "dir": str(s.dir),
            "instructions": s.body.strip()[:MAX_READ_CHARS],
            "files": files,
            "note": "read referenced files with read_file (paths relative to dir); run the "
            "skill's commands with run_command",
        }

    # ---- memory ----
    def memory_index(self) -> str:
        idx = self.memory_dir / "MEMORY.md"
        return idx.read_text().strip() if idx.exists() else ""

    def memory_read(self, name: str | None) -> dict:
        if not name:
            files = sorted(p.stem for p in self.memory_dir.glob("*.md") if p.name != "MEMORY.md")
            return {"index": self.memory_index(), "files": files}
        p = self.memory_dir / f"{_slug(name)}.md"
        if not p.exists():
            raise KeyError(f"no memory named {name}")
        return {"name": p.stem, "text": p.read_text()}

    def memory_write(self, name: str, description: str, content: str) -> dict:
        slug = _slug(name)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        p = self.memory_dir / f"{slug}.md"
        idx = self.memory_dir / "MEMORY.md"
        lines = [
            ln
            for ln in (idx.read_text().splitlines() if idx.exists() else [])
            if f"({slug}.md)" not in ln
        ]
        if content.strip():
            p.write_text(
                f"---\nname: {slug}\ndescription: {description}\n---\n\n{content.strip()}\n"
            )
            lines.append(f"- [{slug}]({slug}.md) — {description}")
            action = "saved"
        else:
            p.unlink(missing_ok=True)
            action = "deleted"
        idx.write_text("\n".join(lines) + ("\n" if lines else ""))
        return {"name": slug, action: True}

    # ---- media ----
    # ---- look: the orchestrator shrinks images / video frames and hands them to the vision LLM
    # ----
    LOOK_MAX = 8
    IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})
    VIDEO_EXT = frozenset({".mp4", ".mov", ".webm", ".mkv", ".m4v"})

    def _expand_look(self, refs: list[str]) -> list[str]:
        store = self.rt.studio.store
        out: list[str] = []
        for r in refs:
            r = r.strip()
            if r.startswith("@round:"):  # a whole round: the first output of each successful task
                rid = r[len("@round:") :]
                if store.get_round(rid) is None:
                    raise ValueError(f"{r}: no such round")
                out += [f"@task:{t['id']}" for t in store.round_tasks(rid) if t["outputs"]]
            else:
                out.append(r)
        return out

    def look(self, run_id: str, refs: list[str], max_side: int = 1024) -> dict:
        """Prepare images for the LLM: images scaled to max_side on the long edge; videos become one sheet of 6 frames."""
        refs = self._expand_look(refs)
        dest = self.workdir(run_id) / "look"
        dest.mkdir(parents=True, exist_ok=True)
        images, errors = [], []
        for i, ref in enumerate(refs):
            if len(images) >= self.LOOK_MAX:
                errors.append({"ref": ref, "error": f"more than {self.LOOK_MAX} images; skipped"})
                continue
            try:
                p = self.readable(run_id, ref)
                if not p.is_file():
                    raise ValueError(f"not a file: {p}")
                out = dest / f"{int(time.time() * 1000)}_{i}.jpg"
                ext = p.suffix.lower()
                fit = (
                    f"scale='min({max_side},iw)':'min({max_side},ih)'"
                    ":force_original_aspect_ratio=decrease"
                )
                if ext in self.IMAGE_EXT:
                    vf = fit
                elif ext in self.VIDEO_EXT:
                    dur = float(
                        subprocess.run(
                            [
                                "ffprobe",
                                "-v",
                                "error",
                                "-show_entries",
                                "format=duration",
                                "-of",
                                "csv=p=0",
                                str(p),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=30,
                            check=True,
                        ).stdout.strip()
                        or 0
                    )
                    side = max_side // 3 * 2
                    vf = (
                        f"fps={6 / max(dur, 0.1):.4f},scale='min({side},iw)':'min({side},ih)'"
                        ":force_original_aspect_ratio=decrease,tile=3x2"
                    )
                else:
                    raise ValueError(f"not an image or video: {p.name}")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(p),
                        "-vf",
                        vf,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "3",
                        str(out),
                    ],
                    capture_output=True,
                    timeout=120,
                    check=True,
                )
                label = ref if ref == str(p) else f"{ref} ({p.name})"
                if ext in self.VIDEO_EXT:
                    label += " — 6 frames, left→right, top→bottom"
                images.append({"ref": label, "file": str(out)})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                errors.append({"ref": ref, "error": str(e)[:300]})
        return {"images": images, "errors": errors}

    def add_media(self, run_id: str, path: str, project_id: str | None) -> dict:
        p = self.readable(run_id, path)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        up = self.rt.studio.save_upload(p.name, p.read_bytes(), project_id)
        return {"upload": f"@upload:{up['id']}", "kind": up["kind"], "path": up["path"]}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not s:
        raise ValueError("memory name must contain letters or digits")
    return s[:60]
