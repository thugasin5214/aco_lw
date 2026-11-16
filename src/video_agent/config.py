"""Runtime configuration: defaults + optional config.toml overrides in the project root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    comfy_url: str = "http://127.0.0.1:8188"
    # ComfyUI's output directory: deleting outputs also deletes the originals this system generated
    # there (only aco_lw/<task id>_*); empty = only data/jobs
    comfy_output_dir: str = ""
    lms_url: str = "http://127.0.0.1:1234"
    llm_model: str = "qwen3.8-27b-splash"
    llm_context: int = 131072
    llm_gpu: str = ""  # lms load --gpu: empty = LM Studio decides; max / off / 0~1
    llm_parallel: int = 0  # lms load --parallel: 0 = LM Studio default
    llm_idle_ttl_s: int = 300  # G0: load ~15 s, unload after 5 min idle
    llm_restore_after_video: bool = True
    # stay loaded longer while a session is active, to keep the prefix cache
    llm_session_ttl_s: int = 1200
    # once the last call's prompt exceeds this, compact older messages
    session_summary_tokens: int = 16000
    session_keep_messages: int = 8  # recent messages kept verbatim when compacting
    # max LLM calls by the agent per request (general tasks take many steps)
    agent_max_steps: int = 40
    agent_skill_dirs: list[str] = field(default_factory=lambda: ["~/.claude/skills"])
    # concurrency of the background tool queue (commands, web reads, MCP calls)
    tool_workers: int = 2
    # MCP servers the agent can use (same format as Claude Code / Codex mcpServers; see mcp_hub.py)
    mcp_servers: dict = field(default_factory=dict)
    planner_fast: bool = False  # append /no_think: measured 14.4 s → 9.3 s, about half the thinking
    # lmstudio | openai | openrouter | anthropic | openai_compatible; switch at runtime with /model
    # (also claude_code / codex)
    planner_backend: str = "lmstudio"
    # when the local LLM lacks resources (e.g. a video render holds the memory) or is unavailable,
    # try in order:
    #   "anthropic" / "openrouter" / "openai_compatible" (online APIs, continue the same run)
    # "claude_code" / "codex" (local agent CLIs + their online models, working through aco_lw MCP
    # tools; new requests only)
    llm_fallback: list[str] = field(default_factory=list)
    # agent approvals: default = approve each item (with "always allow" per kind); bypass = no
    # approvals (hard refusals and online switches still apply).
    # switch at runtime with /permissions (stored in the DB, takes precedence over this)
    permission_mode: str = "default"
    # privacy switch: allow sending conversations to online models / agents (off by default)
    allow_online: bool = False
    # allows online (Claude Code) only for "hand off to the FreeVideo agent" (/fv,
    # delegate_freevideo); everything else follows allow_online
    freevideo_agent_online: bool = False
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"  # low | medium | high | xhigh | max
    # empty: the SDK resolves it from environment variables / `ant auth login`
    anthropic_api_key: str = ""
    # when the safety classifier refuses, the server falls back to the recommended model
    anthropic_fallbacks: bool = True
    # e.g. a model ID on OpenRouter; required when falling back to openrouter
    openrouter_model: str = ""
    openrouter_api_key: str = ""  # empty: read the OPENROUTER_API_KEY environment variable
    openrouter_base_url: str = "https://openrouter.ai/api"
    claude_cmd: str = "claude"
    claude_model: str = ""  # empty: Claude Code's own default model
    codex_cmd: str = "codex"
    codex_model: str = ""
    agent_cli_timeout_s: int = 3600
    openai_base_url: str = ""  # openai_compatible: any OpenAI-compatible service
    # shared by openai (official API, ChatGPT's models) and openai_compatible
    openai_model: str = ""
    openai_api_key: str = ""  # empty: openai reads the OPENAI_API_KEY environment variable
    memory_margin_gib: float = 8.0
    memory_wait_s: float = 600.0
    poll_interval_s: float = 2.0
    task_timeout_s: float = 5400.0
    # the resource scheduler attributes Docker's share of unified memory (read-only probe).
    # Visibility only: on macOS the Docker Desktop VM already counts in psutil used/available,
    # so this does not change the admission math (that would double-count).
    docker_aware: bool = True
    docker_poll_s: float = 5.0  # cache window for the read-only `docker stats` probe
    # use the FreeVideo agent API (character cards, multi-shot films); off in tests
    freevideo_agent: bool = True
    language: str = "en"  # default language of the console / system messages: en | zh
    web_port: int = 8790  # 8765 is the user's other server
    round_size: int = 4
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    templates_dir: Path = field(default_factory=lambda: ROOT / "templates")
    skills_dir: Path = field(default_factory=lambda: ROOT / "skills")
    workflows_dir: Path = field(default_factory=lambda: ROOT / "workflows")
    # set by load(): the settings window writes back here (None in tests = nothing written)
    config_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tasks.sqlite"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "scheduler.lock"


def load(path: Path | None = None) -> Settings:
    path = path or ROOT / "config.toml"
    s = Settings()
    if path.exists():
        raw = tomllib.loads(path.read_text())
        for f in fields(Settings):
            if f.name in raw:
                v = raw[f.name]
                setattr(s, f.name, Path(v) if f.name.endswith("_dir") else v)
    s.config_path = path
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.jobs_dir.mkdir(parents=True, exist_ok=True)
    return s


# ---- console "Settings" window: editable fields (written back to config.toml) ----
# (group, field, description, when it applies)  live = on save; restart = after restarting the
# daemon
EDITABLE: list[tuple[str, str, str, str]] = [
    ("llm", "llm_model", "本地 LLM（LM Studio 模型 key）", "live"),
    ("llm", "llm_context", "上下文长度（tokens）", "live"),
    ("llm", "llm_gpu", "GPU 卸载：空 = LM Studio 自动；max / off / 0~1", "live"),
    ("llm", "llm_parallel", "并发预测数：0 = LM Studio 默认", "live"),
    ("llm", "llm_session_ttl_s", "会话活跃时驻留秒数（按需模式）", "live"),
    ("llm", "llm_idle_ttl_s", "空闲卸载秒数", "live"),
    ("llm", "llm_restore_after_video", "视频渲染结束后重新加载 LLM", "live"),
    ("llm", "planner_fast", "末尾加 /no_think（思考更少、更快）", "live"),
    ("agent", "agent_max_steps", "一次请求最多调用 LLM 的次数", "live"),
    ("agent", "session_summary_tokens", "超过此 prompt 长度压缩较早消息", "live"),
    ("agent", "session_keep_messages", "压缩时保留的最近原话条数", "live"),
    ("agent", "tool_workers", "后台工具并发数", "restart"),
    ("agent", "llm_fallback", "本地 LLM 不可用时的回退顺序（逗号分隔）", "live"),
    ("online", "allow_online", "允许把对话发给在线模型 / 在线 agent", "live"),
    ("online", "freevideo_agent_online", "只为 /fv 放行 Claude Code（在线）", "live"),
    ("online", "openai_model", "OpenAI 模型（openai / openai_compatible 共用）", "live"),
    ("online", "openai_api_key", "OpenAI API key（留空读环境变量）", "live"),
    ("online", "openai_base_url", "openai_compatible 的地址", "live"),
    ("online", "openrouter_model", "OpenRouter 模型 ID", "live"),
    ("online", "openrouter_api_key", "OpenRouter API key（留空读环境变量）", "live"),
    ("online", "anthropic_model", "Claude 模型", "live"),
    ("online", "anthropic_effort", "Claude effort：low / medium / high / xhigh / max", "live"),
    ("online", "anthropic_api_key", "Claude API key（留空由 SDK 解析）", "live"),
    ("online", "claude_model", "Claude Code 模型（留空用默认）", "live"),
    ("online", "codex_model", "Codex 模型（留空用默认）", "live"),
    ("render", "round_size", "每轮候选数", "restart"),
    ("render", "memory_margin_gib", "渲染前预留内存（GiB）", "live"),
    ("render", "memory_wait_s", "等内存的最长秒数", "live"),
    ("render", "docker_aware", "资源曲线计入 Docker 占用（只读）", "live"),
    ("render", "task_timeout_s", "单个任务超时秒数", "live"),
    ("render", "comfy_output_dir", "ComfyUI output 目录（删除产物时连同原图）", "live"),
    ("system", "language", "系统消息语言：en / zh", "live"),
    ("system", "telegram_token", "Telegram bot token", "restart"),
    ("system", "telegram_chat_ids", "Telegram 白名单 chat id（逗号分隔）", "restart"),
]
SECRET = {"openai_api_key", "openrouter_api_key", "anthropic_api_key", "telegram_token"}
CHOICES = {
    "anthropic_effort": ["low", "medium", "high", "xhigh", "max"],
    "language": ["en", "zh"],
}


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def coerce(name: str, value):
    """Convert a value submitted by the console to the field's type; raise ValueError if invalid."""
    f = next((f for f in fields(Settings) if f.name == name), None)
    if f is None or name not in {e[1] for e in EDITABLE}:
        raise ValueError(f"不可编辑的配置项：{name}")
    cur = getattr(Settings(), name)
    if isinstance(cur, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(cur, int):
        return int(value)
    if isinstance(cur, float):
        return float(value)
    if isinstance(cur, list):
        items = value if isinstance(value, list) else str(value).split(",")
        items = [str(x).strip() for x in items if str(x).strip()]
        return [int(x) for x in items] if name == "telegram_chat_ids" else items
    v = str(value).strip()
    if name in CHOICES and v not in CHOICES[name]:
        raise ValueError(f"{name} 只能是 {' / '.join(CHOICES[name])}")
    if name == "llm_gpu" and v not in ("", "max", "off"):
        try:
            if not 0 <= float(v) <= 1:
                raise ValueError
        except ValueError:
            raise ValueError("llm_gpu 只能是 空 / max / off / 0~1") from None
    return v


def save(updates: dict, path: Path) -> None:
    """Change only these top-level keys (everything else, comments and [tables] kept); re-parse to validate before writing."""
    lines = path.read_text().splitlines() if path.exists() else []
    first_table = next(  # only lines before the first [table] header are top-level keys
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith("[") and "=" not in ln),
        len(lines),
    )
    for name, value in updates.items():
        new = f"{name} = {_toml_value(value)}"
        start = next(
            (
                i
                for i, ln in enumerate(lines[:first_table])
                if ln.split("=")[0].strip() == name and "=" in ln
            ),
            None,
        )
        if start is None:
            lines.insert(first_table, new)
            first_table += 1
            continue
        end, depth = (
            start,
            lines[start].split("#")[0].count("[") - lines[start].split("#")[0].count("]"),
        )
        while depth > 0 and end + 1 < len(lines):  # multi-line array
            end += 1
            depth += lines[end].count("[") - lines[end].count("]")
        lines[start : end + 1] = [new]
        first_table -= end - start
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # if we broke it, do not write
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(path)
