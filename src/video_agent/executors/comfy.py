"""ComfyUI executor: render API workflows, validate against /object_info, upload inputs, submit, poll, collect, release."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..comfy import Comfy, ComfyBusy
from ..render import check_graph, render
from . import Poll, Prepared

VIDEO_EXT = {".mp4", ".webm", ".mov", ".gif"}
FREEVIDEO_KEYS = (
    "sample_seconds",
    "request_seconds",
    "ram_peak_bytes",
    "conditioning_cache_hit",
    "result_cache_hit",
    "low_memory",
    "geometry",
)


class ComfyExecutor:
    name = "comfy"

    def __init__(self, comfy: Comfy | None, cfg):
        self.comfy = comfy or Comfy(cfg.comfy_url)
        self._object_info: dict | None = None

    def object_info(self) -> dict:
        if self._object_info is None:
            self._object_info = self.comfy.object_info()
        return self._object_info

    # ---- prepare ----
    def prepare(self, template, task: dict) -> Prepared:
        if template.name == "comfy-raw":
            return self._prepare_raw(task)
        params = dict(task["params"])
        uploads = {}
        for name, slot in template.slots.items():
            if slot.type == "image" and name in params:
                path = Path(params[name]).expanduser()
                if not path.is_file():
                    raise FileNotFoundError(f"{name}: 找不到图片 {path}")
                params[name] = uploads[name] = self.comfy.upload_image(path)
        graph = render(template, params, output_prefix=f"aco_lw/{task['id']}")
        problems = check_graph(graph, self.object_info())
        if problems:
            raise ValueError("；".join(problems))
        return Prepared(
            payload=graph,
            uploads=uploads,
            artifacts={"prompt.json": json.dumps(graph, ensure_ascii=False, indent=2)},
            note=f"节点校验通过；上传 {len(uploads)} 张图",
        )

    def _prepare_raw(self, task: dict) -> Prepared:
        """Any workflow the agent built: validated and submitted as-is, only the save nodes' prefix is changed to this task (easier collection and tracking)."""
        graph = json.loads(task["params"]["graph"])
        for node in graph.values():
            if node.get("class_type", "").startswith("Save") and "filename_prefix" in node.get(
                "inputs", {}
            ):
                node["inputs"]["filename_prefix"] = f"aco_lw/{task['id']}"
        problems = check_graph(graph, self.object_info())
        if problems:
            raise ValueError("；".join(problems))
        return Prepared(
            payload=graph,
            artifacts={"prompt.json": json.dumps(graph, ensure_ascii=False, indent=2)},
            note=f"agent workflow · {len(graph)} nodes · 节点校验通过",
        )

    # ---- submit and poll ----
    def submit(self, prepared: Prepared, job_id: str) -> None:
        self.comfy.submit(prepared.payload, job_id)

    def poll(self, job_id: str) -> Poll:
        entry = self.comfy.history(job_id)
        if entry and entry.get("status", {}).get("completed") is not None:
            ok = entry["status"].get("status_str") == "success"
            return Poll("done" if ok else "failed", result=entry)
        q = self.comfy.queue()
        if any(item[1] == job_id for item in q["queue_running"]):
            return Poll("running")
        if any(item[1] == job_id for item in q["queue_pending"]):
            return Poll("queued")
        return Poll("lost")

    def failure_detail(self, result: Any) -> str:
        msgs = {m[0]: m[1] for m in result.get("status", {}).get("messages", [])}
        err = msgs.get("execution_error", {})
        return str(
            err.get("exception_message") or result.get("status", {}).get("status_str") or "unknown"
        )

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        msgs = {m[0]: m[1] for m in result.get("status", {}).get("messages", [])}
        if "execution_start" in msgs:
            end = msgs.get("execution_success") or msgs.get("execution_error") or {}
            if "timestamp" in end:
                metrics["exec_s"] = round(
                    (end["timestamp"] - msgs["execution_start"]["timestamp"]) / 1000, 1
                )
        outputs = []
        for node, out in (result.get("outputs") or {}).items():
            if out.get("freevideo_summary"):
                fv = out["freevideo_summary"][0]
                metrics["freevideo"] = {k: fv.get(k) for k in FREEVIDEO_KEYS}
            for items in out.values():
                if not isinstance(items, list):
                    continue
                for i, item in enumerate(items):
                    if not (isinstance(item, dict) and "filename" in item):
                        continue
                    name = f"{node}_{i}_{item['filename']}"
                    self.comfy.download(
                        item["filename"],
                        item.get("subfolder", ""),
                        item.get("type", "output"),
                        dest / name,
                    )
                    kind = "video" if Path(name).suffix.lower() in VIDEO_EXT else "image"
                    outputs.append({"file": name, "kind": kind, "node": node, "source": item})
        return outputs

    # ---- cancel, occupancy, release ----
    def cancel(self, job_id: str) -> None:
        self.comfy.interrupt(job_id)

    def queued_payloads(self) -> list[dict]:
        """Workflows of every task running / queued in the executor (including other people's), for cross-process gating."""
        q = self.comfy.queue()
        return [
            item[2]
            for item in q["queue_running"] + q["queue_pending"]
            if len(item) > 2 and isinstance(item[2], dict)
        ]

    def busy(self) -> bool:
        return self.comfy.queue_busy()

    def release(self) -> bool:
        try:
            self.comfy.free()
            return True
        except ComfyBusy:
            return False
