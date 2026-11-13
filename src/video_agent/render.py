"""Parameter validation and template rendering: pure functions, no network.

Tiers decide size / duration; production tasks can only pick a tier. Size-like parameters outside a tier are refused.
"""

from __future__ import annotations

import copy
import hashlib
import json

from .registry import Slot, Template

TIER_KEYS = {"width", "height", "seconds", "frames"}


class ParamError(ValueError):
    pass


def _coerce(name: str, slot: Slot, v):
    if slot.type in ("str", "image"):
        if not isinstance(v, str) or not v:
            raise ParamError(f"{name} 需要非空字符串")
        return v
    if slot.type == "bool":
        if not isinstance(v, bool):
            raise ParamError(f"{name} 需要 true/false")
        return v
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ParamError(f"{name} 需要数字，收到 {v!r}")
    if slot.type == "int":
        if float(v) != int(v):
            raise ParamError(f"{name} 需要整数，收到 {v}")
        v = int(v)
    else:
        v = float(v)
    if slot.min is not None and v < slot.min:
        raise ParamError(f"{name}={v} 小于下限 {slot.min}")
    if slot.max is not None and v > slot.max:
        raise ParamError(f"{name}={v} 超过上限 {slot.max}")
    if slot.multiple_of and v % slot.multiple_of:
        raise ParamError(f"{name}={v} 必须是 {slot.multiple_of} 的倍数")
    return v


def resolve(
    t: Template, params: dict, tier: str | None = None, allow_unverified: bool = False
) -> tuple[str, dict]:
    """Merge tier and user parameters and validate; returns (tier name, full parameters)."""
    tier = tier or t.default_tier
    if tier not in t.tiers:
        raise ParamError(f"{t.name} 没有档位 {tier}；可选：{', '.join(t.tiers)}")
    if not t.tiers[tier].verified and not allow_unverified:
        raise ParamError(
            f"tier {tier} has not been measured yet; approve it explicitly "
            "(console / Telegram button, or --allow-unverified on the CLI)"
        )
    unknown = set(params) - set(t.slots)
    if unknown:
        raise ParamError(f"未知参数：{', '.join(sorted(unknown))}")
    overridden = TIER_KEYS & set(params)
    if overridden:
        raise ParamError(f"{', '.join(sorted(overridden))} 由档位决定，不能直接设置")
    merged = {**t.tiers[tier].values(), **params}
    out = {}
    for name, slot in t.slots.items():
        if name in merged:
            out[name] = _coerce(name, slot, merged[name])
        elif slot.required:
            raise ParamError(f"缺少必填参数 {name}")
    return tier, out


def render(t: Template, params: dict, output_prefix: str | None = None) -> dict:
    """Write parameters into node slots; an image slot's value must already be the filename ComfyUI gave after upload."""
    g = copy.deepcopy(t.graph)
    for name, v in params.items():
        s = t.slots[name]
        g[s.node]["inputs"][s.field] = v
    if output_prefix and t.output_prefix:
        g[t.output_prefix.node]["inputs"][t.output_prefix.field] = output_prefix
    return g


def params_hash(template: Template, params: dict) -> str:
    """Dedup key: template version + full parameters."""
    blob = json.dumps({"t": template.name, "sha": template.sha, "p": params}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def check_graph(graph: dict, object_info: dict) -> list[str]:
    """Validate node types, fields and value ranges against ComfyUI /object_info; returns a list of problems."""
    problems = []
    for nid, node in graph.items():
        info = object_info.get(node["class_type"])
        if info is None:
            problems.append(f"节点 {nid}: 未知类型 {node['class_type']}")
            continue
        spec = {**info["input"].get("required", {}), **info["input"].get("optional", {})}
        for field, v in node["inputs"].items():
            head = field.split(".", 1)[0]
            if field not in spec and "." in field and head in spec:
                # V3 dynamic inputs (sub-fields of autogrow / dynamic combo, e.g.
                # ref_images.ref_image_0, format.codec):
                # /object_info lists only the parent field; only validate links
                if isinstance(v, list) and v and str(v[0]) not in graph:
                    problems.append(f"节点 {nid}.{field} 连到不存在的节点 {v[0]}")
                continue
            if field not in spec:
                problems.append(f"节点 {nid}: {node['class_type']} 没有字段 {field}")
                continue
            if isinstance(v, list):  # link: must point to an existing node
                if v and str(v[0]) not in graph:
                    problems.append(f"节点 {nid}.{field} 连到不存在的节点 {v[0]}")
                continue
            kind = spec[field][0]
            opts = spec[field][1] if len(spec[field]) > 1 else {}
            if kind == "COMBO":
                kind = opts.get("options", [])
            if kind in ("INT", "FLOAT") and isinstance(v, int | float):
                if "min" in opts and v < opts["min"]:
                    problems.append(f"节点 {nid}.{field}={v} 小于 {opts['min']}")
                if "max" in opts and v > opts["max"]:
                    problems.append(f"节点 {nid}.{field}={v} 超过 {opts['max']}")
            elif isinstance(kind, list) and kind and v not in kind and field != "image":
                problems.append(f"节点 {nid}.{field}={v!r} 不在可选值中")
    return problems
