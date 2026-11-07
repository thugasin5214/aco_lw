"""通用工具层：agent 是大脑，只提出 tool call；编排器（这里）执行，结果落库后择机交回。

- 慢的工具（读网页、执行命令）放进后台工具队列（ToolJobs），不占 agent 线程、不需要 LLM 在线：
  跑完把结果写进对应的工具消息；LLM 此时若被卸载（视频渲染占内存），运行停在 waiting_resources，
  内存空出来再把结果交回去（与等渲染完全同一条路径）。
- 执行命令一律先经你批准（Telegram / 控制台按钮），除非命令以 policies.yaml 里
  `run_command.auto_allow` 列出的前缀开头（默认空）。批准后仍拒绝：直连 ComfyUI / FreeVideo、
  读 FreeVideo token——生成相关的副作用只走编排器的生成工具（资源闸门、只动自己的任务）。
- 文件：读限于 data/ 与 skill 目录；写限于本次运行的工作目录 data/agent-work/<run>/。
- skill：兼容 Claude Code 的 SKILL.md（内置 skills/、data/skills/、`agent_skill_dirs`，默认含
  ~/.claude/skills）。系统提示只列名字与描述，agent 需要时 load_skill 读全文，按其中说明用工具执行。
- 记忆：agent 自己的长期记忆 data/agent_memory/*.md（MEMORY.md 为索引，进系统提示）。
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

OUT_TAIL = 6000  # 交回 LLM 的输出尾部字符数；全文留在日志文件
MAX_TIMEOUT_S = 3600
MAX_READ_CHARS = 20000
# 批准了也不执行：生成的副作用只走编排器；token 只在编排器进程内读取
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
    """后台工具队列：慢工具在这里跑，跑完调用 done(key, result)。"""

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

    # ---- 路径 ----
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
        if path.startswith("@task:"):  # 任务产物：第一个输出文件
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

    # ---- 命令 ----
    def auto_allowed(self, command: str) -> bool:
        prefixes = self.rt.policies.tool("run_command").get("auto_allow") or []
        c = command.strip()
        # 有管道 / 串联 / 重定向 / 子命令的不自动放行：前缀只对单条命令有意义
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

    # ---- 网页 ----
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

    # ---- 文件 ----
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

    # ---- 记忆 ----
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

    # ---- 媒体 ----
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
