"""运行配置：默认值 + 可选的项目根目录 config.toml 覆盖。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    comfy_url: str = "http://127.0.0.1:8188"
    # ComfyUI 的 output 目录：删除产物时连同本系统在那里生成的原图一起删（只删 aco_lw/<任务ID>_*）；留空则只删 data/jobs
    comfy_output_dir: str = ""
    lms_url: str = "http://127.0.0.1:1234"
    llm_model: str = "qwen3.8-27b-splash"
    llm_context: int = 131072
    llm_gpu: str = ""  # lms load --gpu：空 = LM Studio 自动；max / off / 0~1
    llm_parallel: int = 0  # lms load --parallel：0 = LM Studio 默认
    llm_idle_ttl_s: int = 300  # G0：加载约 15 s，空闲 5 min 卸载
    llm_restore_after_video: bool = True
    llm_session_ttl_s: int = 1200  # 会话活跃时延长驻留，保住前缀缓存
    session_summary_tokens: int = 16000  # 上一次调用超过此 prompt 长度即压缩较早消息
    session_keep_messages: int = 8  # 压缩时保留的最近原话条数
    agent_max_steps: int = 40  # 一次请求里 agent 最多调用 LLM 的次数（通用任务步骤多）
    agent_skill_dirs: list[str] = field(default_factory=lambda: ["~/.claude/skills"])
    tool_workers: int = 2  # 后台工具队列（命令、读网页、MCP 调用）并发数
    # agent 可用的 MCP 服务（格式同 Claude Code / Codex 的 mcpServers；见 mcp_hub.py）
    mcp_servers: dict = field(default_factory=dict)
    planner_fast: bool = False  # 末尾加 /no_think：实测 14.4 s → 9.3 s，思考约减半
    # lmstudio | openai | openrouter | anthropic | openai_compatible；运行中可用 /model 切换（含 claude_code / codex）
    planner_backend: str = "lmstudio"
    # 本地 LLM 资源不够（如视频渲染占着内存）或不可用时，按顺序尝试：
    #   "anthropic" / "openrouter" / "openai_compatible"（在线 API，接着同一次运行继续）
    #   "claude_code" / "codex"（本机的 agent CLI + 它们的在线模型，经 aco_lw MCP 工具工作，只接新请求）
    llm_fallback: list[str] = field(default_factory=list)
    # agent 审批：default 逐条批准（可“始终允许”某类）；bypass 不再审批（硬拒绝与在线开关照旧）。
    # 运行中用 /permissions 切换（存库，优先于这里）
    permission_mode: str = "default"
    allow_online: bool = False  # 隐私开关：允许把对话发给在线模型 / 在线 agent（默认关）
    # 只为“交给 FreeVideo agent”（/fv、delegate_freevideo）放行在线（Claude Code）；其他仍按 allow_online
    freevideo_agent_online: bool = False
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
    openai_base_url: str = ""  # openai_compatible：任意 OpenAI 兼容服务
    openai_model: str = ""  # openai（官方 API，ChatGPT 的模型）与 openai_compatible 共用
    openai_api_key: str = ""  # 留空：openai 读环境变量 OPENAI_API_KEY
    memory_margin_gib: float = 8.0
    memory_wait_s: float = 600.0
    poll_interval_s: float = 2.0
    task_timeout_s: float = 5400.0
    freevideo_agent: bool = True  # 用 FreeVideo agent API（角色卡、多镜头电影）；测试里关掉
    language: str = "en"  # 控制台 / 系统消息默认语言：en | zh
    web_port: int = 8790  # 8765 是用户另一个 server
    round_size: int = 4
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    templates_dir: Path = field(default_factory=lambda: ROOT / "templates")
    skills_dir: Path = field(default_factory=lambda: ROOT / "skills")
    workflows_dir: Path = field(default_factory=lambda: ROOT / "workflows")
    config_path: Path | None = None  # 由 load() 填：设置窗口写回这里（测试里为空 = 不落盘）

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


# ---- 控制台“设置”窗口：可编辑的字段（写回 config.toml）----
# (分组, 字段, 说明, 生效方式)  生效方式：live = 保存即生效；restart = 重启 daemon 后生效
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
    """把控制台提交的值转成字段的类型；不合法抛 ValueError。"""
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
    """只改顶层的这些键（其余内容、注释、[表] 原样保留），写前重新解析校验。"""
    lines = path.read_text().splitlines() if path.exists() else []
    first_table = next(  # 第一个 [表] 头之前才是顶层键
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
        while depth > 0 and end + 1 < len(lines):  # 多行数组
            end += 1
            depth += lines[end].count("[") - lines[end].count("]")
        lines[start : end + 1] = [new]
        first_table -= end - start
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # 写坏了就不写
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(path)
