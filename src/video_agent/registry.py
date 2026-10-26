"""模板注册表与模型注册表。

模板目录 `templates/<name>/`：`api.json`（ComfyUI API 格式）+ `template.yaml`（槽位、档位）。
新模型 = 新模板目录 + `models.yaml` 一条实测记录，编排器代码不变。
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
    release_after: Literal["keep", "free"] = "free"
    output_prefix: NodeField | None = None
    slots: dict[str, Slot]
    tiers: dict[str, Tier]
    default_tier: str
    graph: dict = {}
    sha: str = ""


class ModelInfo(BaseModel):
    kind: Literal["image", "video"]
    peak_gib: float
    measured: bool = False
    exclusive_llm: bool = False


def load_template(dir_: Path) -> Template:
    raw = yaml.safe_load((dir_ / "template.yaml").read_text())
    api = (dir_ / "api.json").read_bytes()
    t = Template(name=dir_.name, graph=json.loads(api), **raw)
    t.sha = hashlib.sha256(api + (dir_ / "template.yaml").read_bytes()).hexdigest()[:12]
    for name, s in t.slots.items():
        node = t.graph.get(s.node)
        if node is None or s.field not in node["inputs"]:
            raise ValueError(f"{t.name}: 槽位 {name} 指向不存在的 {s.node}.{s.field}")
    if t.default_tier not in t.tiers:
        raise ValueError(f"{t.name}: default_tier {t.default_tier} 未定义")
    return t


class Registry:
    def __init__(self, templates_dir: Path):
        self.templates = {
            d.name: load_template(d)
            for d in sorted(templates_dir.iterdir())
            if (d / "template.yaml").exists()
        }
        models = yaml.safe_load((templates_dir / "models.yaml").read_text())
        self.models = {k: ModelInfo(**v) for k, v in models.items()}
        for t in self.templates.values():
            if t.model not in self.models:
                raise ValueError(f"{t.name}: 模型 {t.model} 未在 models.yaml 登记")

    def template(self, name: str) -> Template:
        if name not in self.templates:
            raise KeyError(f"未知模板 {name}；可选：{', '.join(self.templates)}")
        return self.templates[name]

    def model_of(self, t: Template) -> ModelInfo:
        return self.models[t.model]
