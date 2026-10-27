"""运行配置：默认值 + 可选的项目根目录 config.toml 覆盖。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    comfy_url: str = "http://127.0.0.1:8188"
    lms_url: str = "http://127.0.0.1:1234"
    llm_model: str = "qwen3.8-27b-splash"
    llm_context: int = 131072
    llm_idle_ttl_s: int = 300  # G0：加载约 15 s，空闲 5 min 卸载
    llm_restore_after_video: bool = True
    llm_session_ttl_s: int = 1200  # 会话活跃时延长驻留，保住前缀缓存
    session_summary_tokens: int = 16000  # 上一次调用超过此 prompt 长度即压缩较早消息
    session_keep_messages: int = 8  # 压缩时保留的最近原话条数
    agent_max_steps: int = 16  # 一次请求里 agent 最多调用 LLM 的次数
    planner_fast: bool = False  # 末尾加 /no_think：实测 14.4 s → 9.3 s，思考约减半
    planner_backend: str = "lmstudio"  # lmstudio | openai_compatible | openrouter | anthropic
    # 本地 LLM 资源不够（如视频渲染占着内存）或不可用时，按顺序尝试：
    #   "anthropic" / "openrouter" / "openai_compatible"（在线 API，接着同一次运行继续）
    #   "claude_code" / "codex"（本机的 agent CLI + 它们的在线模型，经 aco_lw MCP 工具工作，只接新请求）
    llm_fallback: list[str] = field(default_factory=list)
    allow_online: bool = False  # 隐私开关：允许把对话发给在线模型 / 在线 agent（默认关）
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"  # low | medium | high | xhigh | max
    anthropic_api_key: str = ""  # 留空：由 SDK 按环境变量 / `ant auth login` 解析
    anthropic_fallbacks: bool = True  # 安全分类器拒答时由服务端回退到推荐模型
    openrouter_model: str = ""  # 例如 OpenRouter 上的模型 ID；用 openrouter 回退时必填
    openrouter_api_key: str = ""  # 留空：读环境变量 OPENROUTER_API_KEY
    openrouter_base_url: str = "https://openrouter.ai/api"
    claude_cmd: str = "claude"
    claude_model: str = ""  # 留空用 Claude Code 自己的默认模型
    codex_cmd: str = "codex"
    codex_model: str = ""
    agent_cli_timeout_s: int = 3600
    openai_base_url: str = ""
    openai_model: str = ""
    openai_api_key: str = ""
    memory_margin_gib: float = 8.0
    memory_wait_s: float = 600.0
    poll_interval_s: float = 2.0
    task_timeout_s: float = 5400.0
    language: str = "en"  # 控制台 / 系统消息默认语言：en | zh
    web_port: int = 8790  # 8765 是用户另一个 server
    round_size: int = 4
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    templates_dir: Path = field(default_factory=lambda: ROOT / "templates")
    skills_dir: Path = field(default_factory=lambda: ROOT / "skills")
    workflows_dir: Path = field(default_factory=lambda: ROOT / "workflows")

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
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.jobs_dir.mkdir(parents=True, exist_ok=True)
    return s
