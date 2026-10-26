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
    memory_margin_gib: float = 8.0
    memory_wait_s: float = 600.0
    poll_interval_s: float = 2.0
    task_timeout_s: float = 5400.0
    web_port: int = 8790  # 8765 是用户另一个 server
    round_size: int = 4
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    templates_dir: Path = field(default_factory=lambda: ROOT / "templates")

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
