"""Template registry and model registry.

Template directory `templates/<name>/`: `api.json` (ComfyUI API format) + `template.yaml` (slots, tiers).
A new model = a new template directory + one measured entry in `models.yaml`; orchestrator code does not change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    field: str
    type: Literal["str", "int", "float", "bool", "image"]
    min: float | None = None
    max: float | None = None
    multiple_of: int | None = None
    required: bool = False


class Tier(BaseModel):
    model_config = ConfigDict(extra="allow")

    verified: bool = False

    def values(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if k != "verified"}


class NodeField(BaseModel):
    node: str
    field: str


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    title: str
    kind: Literal["image", "video"]
    model: str
    source: str = ""
    executor: str = "comfy"  # which executor runs it (executors/)
    release_after: Literal["keep", "free"] = "free"
    output_prefix: NodeField | None = None
    slots: dict[str, Slot]
    tiers: dict[str, Tier]
    default_tier: str
    graph: dict = {}
    sha: str = ""
    # built-in / promoted by the user / candidate variant
    origin: Literal["builtin", "user", "variant"] = "builtin"
    meta: dict = {}  # variant metadata (base template, project, status, diff, trial results)


class ModelInfo(BaseModel):
    kind: Literal["image", "video"]
    peak_gib: float
    measured: bool = False
    exclusive_llm: bool = False
    # engine adapts to available memory (e.g. FreeVideo): measured peak != minimum requirement
    adaptive: bool = False
    resource: Literal["memory", "cpu"] = "memory"  # cpu: skips the memory gate (e.g. FFmpeg)
    exec_s: float | None = None  # default duration when there is no history (estimate)


def load_template(dir_: Path, origin: str = "builtin") -> Template:
    raw = yaml.safe_load((dir_ / "template.yaml").read_text())
    api = (dir_ / "api.json").read_bytes()
    meta_path = dir_ / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    name = raw.pop("name", dir_.name)
    t = Template(name=name, graph=json.loads(api), origin=origin, meta=meta, **raw)
    t.sha = hashlib.sha256(api + (dir_ / "template.yaml").read_bytes()).hexdigest()[:12]
    for slot_name, s in t.slots.items():
        node = t.graph.get(s.node)
        if node is None or s.field not in node["inputs"]:
            raise ValueError(f"{t.name}: 槽位 {slot_name} 指向不存在的 {s.node}.{s.field}")
    if t.default_tier not in t.tiers:
        raise ValueError(f"{t.name}: default_tier {t.default_tier} 未定义")
    return t


class Registry:
    """Built-in templates (repo templates/) + user-promoted templates (data/templates/) + candidate variants (data/variants/).

    Candidate variants load only in state candidate / trial_ok; the planner does not see them by default (see builtin_and_user).
    """

    def __init__(
        self, templates_dir: Path, user_dir: Path | None = None, variants_dir: Path | None = None
    ):
        self.templates_dir, self.user_dir, self.variants_dir = templates_dir, user_dir, variants_dir
        self.reload()

    @classmethod
    def from_settings(cls, cfg) -> Registry:
        return cls(cfg.templates_dir, cfg.data_dir / "templates", cfg.data_dir / "variants")

    def reload(self) -> None:
        templates: dict[str, Template] = {}
        sources = [
            (self.templates_dir, "builtin"),
            (self.user_dir, "user"),
            (self.variants_dir, "variant"),
        ]
        for root, origin in sources:
            if not root or not root.exists():
                continue
            for d in sorted(root.iterdir()):
                if not (d / "template.yaml").exists():
                    continue
                t = load_template(d, origin)
                if origin == "variant" and t.meta.get("status") not in ("candidate", "trial_ok"):
                    continue
                templates[t.name] = t
        models = yaml.safe_load((self.templates_dir / "models.yaml").read_text())
        self.models = {k: ModelInfo(**v) for k, v in models.items()}
        for t in templates.values():
            if t.model not in self.models:
                raise ValueError(f"{t.name}: 模型 {t.model} 未在 models.yaml 登记")
        self.templates = templates

    def builtin_and_user(self) -> dict[str, Template]:
        """Templates available to the planner: excludes unpromoted candidate variants."""
        return {k: t for k, t in self.templates.items() if t.origin != "variant"}

    def template(self, name: str) -> Template:
        if name not in self.templates:
            raise KeyError(f"未知模板 {name}；可选：{', '.join(self.templates)}")
        return self.templates[name]

    def model_of(self, t: Template) -> ModelInfo:
        return self.models[t.model]
