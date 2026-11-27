"""Your ComfyUI workflows as a catalog the agent can see and run.

The catalog lists the canvas workflows (the JSON files ComfyUI saves) in the folders configured in
`data/comfy_workflows.yaml` — by default ComfyUI's own `user/default/workflows` next to
`comfy_output_dir`. For each one the agent gets your notes (Note / MarkdownNote nodes), the models
and LoRAs it loads and its main settings, and can run it with a few values changed
(`{"136.length": 124}`). Conversion to the API format is done here, against ComfyUI's live
`/object_info`, the way the ComfyUI frontend does it: notes, reroutes and primitive nodes are
resolved, muted nodes dropped, bypassed nodes passed through, and the extra widget values the
frontend stores (seed "control after generate", image "upload") are skipped.

```yaml
dirs: [~/ComfyUI/user/default/workflows]     # every *.json in these folders is listed
workflows:                                   # optional, per file name (without .json)
  H3-Native-Ref2VA-20step: {note: "verified at 768x1344, 124 frames", verified: true}
  Old-Experiment: {hidden: true}
```
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
NOTE_TYPES = {"Note", "MarkdownNote"}
SEED_NAMES = {"seed", "noise_seed"}
MUTED, BYPASSED = 2, 4
# canvas values worth showing the agent before it runs a workflow
KEY_INPUTS = {
    "width",
    "height",
    "length",
    "frames",
    "num_frames",
    "fps",
    "frame_rate",
    "steps",
    "cfg",
    "denoise",
    "megapixels",
    "aspect_ratio",
    "sampler_name",
    "scheduler",
    "strength_model",
    "strength",
    "shift",
    "upscale_factor",
    "two_pass",
}


class WorkflowError(ValueError):
    pass


def _is_widget(spec: list) -> bool:
    t = spec[0] if spec else None
    return isinstance(t, list) or t in WIDGET_TYPES


def _opts(spec: list) -> dict:
    return spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}


def _order(info: dict) -> list[tuple[str, list]]:
    inputs = info.get("input") or {}
    order = info.get("input_order") or {k: list(v) for k, v in inputs.items()}
    out = []
    for section in ("required", "optional"):
        for name in order.get(section) or []:
            spec = (inputs.get(section) or {}).get(name)
            if spec is not None:
                out.append((name, spec))
    return out


def notes(wf: dict) -> list[str]:
    return [
        str((n.get("widgets_values") or [""])[0]).strip()
        for n in wf.get("nodes") or []
        if n.get("type") in NOTE_TYPES and n.get("widgets_values")
    ]


def canvas_to_api(wf: dict, object_info: dict) -> dict:
    """A canvas workflow (as ComfyUI saves it) → an API-format graph."""
    if (wf.get("definitions") or {}).get("subgraphs"):
        raise WorkflowError(
            "workflows with subgraphs are not supported yet; unpack them in ComfyUI"
        )
    nodes = {n["id"]: n for n in wf.get("nodes") or []}
    links = {
        lk[0]: (lk[1], lk[2])
        for lk in wf.get("links") or []
        if isinstance(lk, list) and len(lk) >= 5
    }

    def source(link_id, seen=()) -> list | Any | None:
        """Follow a link back to a real output: [node_id, slot], or a primitive's value."""
        if link_id is None or link_id not in links or link_id in seen:
            return None
        nid, slot = links[link_id]
        n = nodes.get(nid)
        if n is None:
            return None
        if n.get("type") == "Reroute":
            ins = n.get("inputs") or []
            return source(ins[0].get("link") if ins else None, (*seen, link_id))
        if n.get("type") == "PrimitiveNode":
            return {"value": (n.get("widgets_values") or [None])[0]}
        if n.get("mode") == MUTED:
            return None
        if n.get("mode") == BYPASSED:  # pass through the first input of the same type
            out_type = ((n.get("outputs") or [{}])[slot] or {}).get("type")
            for i in n.get("inputs") or []:
                if i.get("link") is not None and i.get("type") == out_type:
                    return source(i["link"], (*seen, link_id))
            return None
        return [str(nid), slot]

    graph: dict[str, dict] = {}
    for nid, n in nodes.items():
        kind = n.get("type")
        if (
            kind in NOTE_TYPES
            or kind in ("Reroute", "PrimitiveNode")
            or n.get("mode")
            in (
                MUTED,
                BYPASSED,
            )
        ):
            continue
        info = object_info.get(kind)
        if info is None:
            raise WorkflowError(f"node {nid}: ComfyUI has no node type {kind}")
        canvas_inputs = {i["name"]: i for i in n.get("inputs") or []}
        values = n.get("widgets_values")
        named = values if isinstance(values, dict) else None
        seq = list(values) if isinstance(values, list) else []
        pos = 0
        inputs: dict[str, Any] = {}
        used = set()
        for name, spec in _order(info):
            used.add(name)
            link = (canvas_inputs.get(name) or {}).get("link")
            value = None
            if _is_widget(spec):
                if named is not None:
                    value = named.get(name)
                elif pos < len(seq):
                    value = seq[pos]
                    pos += 1
                opts = _opts(spec)
                if named is None and (
                    opts.get("control_after_generate") or (name in SEED_NAMES and spec[0] == "INT")
                ):
                    pos += 1  # "fixed" / "randomize" — a frontend-only widget
                # the "choose file to upload" button's value comes after all real widgets, so the
                # values left over at the end are simply ignored
            src = source(link)
            if isinstance(src, dict):  # a primitive node feeds this widget
                inputs[name] = src["value"]
            elif src is not None:
                inputs[name] = src
            elif value is not None:
                inputs[name] = value
        # dynamic inputs (e.g. "ref_images.ref_image_0") that object_info lists only as a group
        for name, i in canvas_inputs.items():
            if name in used or i.get("link") is None:
                continue
            src = source(i["link"])
            if isinstance(src, dict):
                inputs[name] = src["value"]
            elif src is not None:
                inputs[name] = src
        graph[str(nid)] = {"class_type": kind, "inputs": inputs}
        if n.get("title"):
            graph[str(nid)]["_meta"] = {"title": n["title"]}
    # links to nodes that were dropped (muted / missing): remove so validation reports a clear
    # missing input instead of a dangling link
    for node in graph.values():
        for k, v in list(node["inputs"].items()):
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in graph:
                del node["inputs"][k]
    return graph


def apply_overrides(graph: dict, changes: dict | None) -> dict:
    """`{"136.length": 124}` sets node 136's input; `{"length": 124}` works when only one node
    has that input. Values that are links cannot be overridden this way."""
    g = json.loads(json.dumps(graph))
    for key, value in (changes or {}).items():
        nid, _, field = str(key).rpartition(".")
        if nid and nid in g:
            targets = [nid]
        else:
            field = str(key)
            targets = [k for k, n in g.items() if field in n["inputs"]]
            if len(targets) != 1:
                raise WorkflowError(
                    f"{key}: {'no node' if not targets else 'several nodes'} with that input; "
                    "use node_id.input"
                )
        node = g[targets[0]]
        if isinstance(node["inputs"].get(field), list):
            raise WorkflowError(f"{key} is connected to another node, not a value")
        node["inputs"][field] = value
    return g


def summary(graph: dict) -> dict:
    """What the agent should know before running it: models, LoRAs, the main settings."""
    models, loras, settings = [], [], {}
    for nid, n in graph.items():
        for k, v in n["inputs"].items():
            if isinstance(v, list):
                continue
            if k == "lora_name" or "lora" in n["class_type"].lower() and k.endswith("_name"):
                loras.append(
                    {
                        "node": nid,
                        "lora": v,
                        "strength": n["inputs"].get("strength_model", n["inputs"].get("strength")),
                    }
                )
            elif k.endswith("_name") and isinstance(v, str) and "." in v:
                models.append(f"{nid}.{k}={v}")
            elif k in KEY_INPUTS:
                settings[f"{nid}.{k}"] = v
            elif k == "value" and isinstance(v, str) and len(v) > 40:
                settings[f"{nid}.value"] = f"<text {len(v)} chars>"
    return {"models": models, "loras": loras, "settings": settings}


class Catalog:
    def __init__(self, data_dir: Path, comfy_output_dir: str = ""):
        self.path = data_dir / "comfy_workflows.yaml"
        self.default_dir = (
            Path(comfy_output_dir).expanduser().parent / "user" / "default" / "workflows"
            if comfy_output_dir
            else None
        )

    def config(self) -> dict:
        if not self.path.exists():
            return {}
        data = yaml.safe_load(self.path.read_text()) or {}
        if not isinstance(data, dict):
            raise WorkflowError("comfy_workflows.yaml must be a mapping (dirs, workflows)")
        return data

    def dirs(self) -> list[Path]:
        cfg = self.config()
        dirs = [Path(os.path.expanduser(str(d))) for d in cfg.get("dirs") or []]
        if not dirs and self.default_dir is not None:
            dirs = [self.default_dir]
        return [d for d in dirs if d.is_dir()]

    def files(self) -> dict[str, Path]:
        hidden = {
            k for k, v in (self.config().get("workflows") or {}).items() if (v or {}).get("hidden")
        }
        out: dict[str, Path] = {}
        for d in self.dirs():
            for f in sorted(d.glob("*.json")):
                if f.stem not in hidden:
                    out.setdefault(f.stem, f)
        return out

    def canvas(self, name: str) -> dict:
        f = self.files().get(name)
        if f is None:
            raise WorkflowError(f"no ComfyUI workflow {name!r}; see list_comfy_workflows")
        wf = json.loads(f.read_text())
        if "nodes" not in wf:
            raise WorkflowError(f"{name} is not a canvas workflow")
        return wf

    def entry(self, name: str, object_info: dict | None = None) -> dict:
        f = self.files()[name]
        wf = self.canvas(name)
        extra = (self.config().get("workflows") or {}).get(name) or {}
        out = {
            "name": name,
            "file": str(f),
            "modified": int(f.stat().st_mtime),
            "notes": [n[:800] for n in notes(wf)],
            "your_note": extra.get("note"),
            "verified": bool(extra.get("verified")),
        }
        if object_info is not None:
            try:
                out.update(summary(canvas_to_api(wf, object_info)))
            except WorkflowError as e:
                out["error"] = str(e)
        return out

    def list(self, object_info: dict | None = None) -> list[dict]:
        rows = [self.entry(n, object_info) for n in self.files()]
        return sorted(rows, key=lambda r: -r["modified"])

    def index(self) -> str:
        """One line per workflow for the agent's prompt (newest first; no live data, so the prompt
        prefix only changes when you change the folder)."""
        lines = []
        for r in self.list():
            first = next((ln for n in r["notes"] for ln in n.splitlines() if ln.strip()), "")
            tag = " ✓verified" if r["verified"] else ""
            lines.append(f"- {r['name']}{tag}: {(r['your_note'] or first).lstrip('# ')[:140]}")
        return "\n".join(lines[:40])

    def api(self, name: str, object_info: dict, changes: dict | None = None) -> dict:
        return apply_overrides(canvas_to_api(self.canvas(name), object_info), changes)
