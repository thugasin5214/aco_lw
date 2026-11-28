"""What the agent can generate with beyond the templates: the LoRAs installed in ComfyUI.

The list is read from ComfyUI's `models/loras` folder (next to `comfy_output_dir`); each file's
safetensors header tells its base model. Your own notes (who / what style / strength / trigger word)
come from `data/loras.yaml` (gitignored, personal):

    klein4b-example.safetensors:
      person: the character it was trained on
      style: photo
      strength: 0.5
      trigger: null
      note: free text
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

HEADER_MAX = 50 * 1024 * 1024  # a safetensors header larger than this is not a real header
EXT = (".safetensors",)


def lora_dir(cfg) -> Path | None:
    out = getattr(cfg, "comfy_output_dir", "") or ""
    if not out:
        return None
    d = Path(out).expanduser().parent / "models" / "loras"
    return d if d.is_dir() else None


def read_meta(path: Path) -> dict:
    """The `__metadata__` block of a safetensors file (empty when unreadable)."""
    try:
        with path.open("rb") as fh:
            (n,) = struct.unpack("<Q", fh.read(8))
            if n > HEADER_MAX:
                return {}
            return json.loads(fh.read(n)).get("__metadata__") or {}
    except (OSError, ValueError, struct.error):
        return {}


def base_of(meta: dict) -> str:
    """A normalised base-model tag from kohya / modelspec / converter metadata."""
    raw = " ".join(
        str(meta.get(k, ""))
        for k in ("ss_base_model_version", "modelspec.architecture", "base_model")
    ).lower()
    if "klein" in raw and "4b" in raw:
        return "flux2-klein-4b"
    if "minimax-h3" in raw or "minimax_h3" in raw:
        return "minimax-h3"
    return ""


class LoraCatalog:
    def __init__(self, cfg, registry):
        self.cfg, self.registry = cfg, registry
        self._cache: tuple[tuple, str] | None = None

    def notes(self) -> dict:
        p = self.cfg.data_dir / "loras.yaml"
        if not p.is_file():
            return {}
        try:
            return yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as e:
            log.warning("data/loras.yaml is not valid YAML: %s", e)
            return {}

    def templates_for(self, base: str) -> list[str]:
        """Templates with a `lora` slot whose model takes LoRAs of this base."""
        out = []
        for t in self.registry.builtin_and_user().values():
            model = self.registry.models.get(t.model)
            if "lora" in t.slots and model and base and base in (model.lora_base or []):
                out.append(t.name)
        return sorted(out)

    def items(self) -> list[dict]:
        d = lora_dir(self.cfg)
        if d is None:
            return []
        notes = self.notes()
        rows = []
        for f in sorted(p for p in d.rglob("*") if p.suffix in EXT):
            name = f.relative_to(d).as_posix()
            # many community LoRAs carry no base-model metadata: data/loras.yaml `base:` fills it in
            base = (notes.get(name) or {}).get("base") or base_of(read_meta(f))
            rows.append(
                {
                    "file": name,
                    "base": base,
                    "templates": self.templates_for(base),
                    **(notes.get(name) or {}),
                }
            )
        return rows

    def text(self) -> str:
        """The prompt section; cached until a file or the notes change (keeps the prompt prefix stable)."""
        d = lora_dir(self.cfg)
        if d is None:
            return ""
        notes = self.cfg.data_dir / "loras.yaml"
        key = tuple(
            (p.name, p.stat().st_mtime)
            for p in [*sorted(d.rglob("*.safetensors")), notes]
            if p.exists()
        )
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        lines = []
        for r in self.items():
            bits = [f"base {r['base'] or 'unknown'}"]
            if r["templates"]:
                bits.append(
                    "use with " + ", ".join(r["templates"]) + " (params lora / lora_strength)"
                )
            else:
                bits.append("no template takes it")
            for k in ("kind", "person", "style", "strength", "trigger", "note"):
                if r.get(k) not in (None, ""):
                    bits.append(f"{k}: {r[k]}")
            lines.append(f"- {r['file']} — " + "; ".join(bits))
        text = "\n".join(lines)
        self._cache = (key, text)
        return text
