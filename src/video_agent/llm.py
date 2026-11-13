"""LM Studio residency control (only via the `lms` CLI). G0: load ~15 s, unload 0.3 s, --ttl works."""

from __future__ import annotations

import json
import shutil
import subprocess


def available() -> bool:
    return shutil.which("lms") is not None


def _lms(*args: str) -> str:
    return subprocess.run(["lms", *args], capture_output=True, text=True, check=True).stdout


def loaded(model: str) -> dict | None:
    if not available():
        return None
    try:
        models = json.loads(_lms("ps", "--json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    return next((m for m in models if m.get("identifier") == model), None)


def unload(model: str) -> None:
    _lms("unload", model)


def load(
    model: str, context: int, ttl_s: int | None = None, gpu: str = "", parallel: int = 0
) -> None:
    args = ["load", model, "-y", "--identifier", model, "-c", str(context)]
    if ttl_s:
        args += ["--ttl", str(ttl_s)]
    if gpu:
        args += ["--gpu", str(gpu)]
    if parallel:
        args += ["--parallel", str(parallel)]
    _lms(*args)


def models() -> list[dict]:
    """LLMs downloaded in LM Studio (for the settings window's drop-down)."""
    if not available():
        return []
    try:
        rows = json.loads(_lms("ls", "--json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    return [
        {
            "key": m.get("modelKey"),
            "max_context": m.get("maxContextLength"),
            "vision": bool(m.get("vision")),
            "size": m.get("sizeBytes"),
        }
        for m in rows
        if m.get("type") == "llm"
    ]


def loaded_all() -> list[dict]:
    if not available():
        return []
    try:
        return json.loads(_lms("ps", "--json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []


_VISION: dict[str, tuple[float, bool | None]] = {}


def supports_vision(model: str) -> bool | None:
    """Vision support as reported by LM Studio (None = unknown). Cached 60 s so a model switch is picked up quickly."""
    import time

    hit = _VISION.get(model)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    ans = None
    key = model.lower()
    for m in [*loaded_all(), *_ls()]:
        if key in (str(m.get("identifier", "")).lower(), str(m.get("modelKey", "")).lower()):
            ans = bool(m.get("vision"))
            break
    _VISION[model] = (time.monotonic(), ans)
    return ans


def _ls() -> list[dict]:
    if not available():
        return []
    try:
        return json.loads(_lms("ls", "--json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []
