"""Bridge to a third-party ComfyUI agent tool (artokun/comfyui-mcp): started by the orchestrator, calls allowed by "tool + action" class.

User decision 2026-10-27: the agent may explore, build and trial-run; installing nodes / downloading models needs the user's approval;
execution calls are scheduled by our orchestrator (they become trackable tasks, subject to pause and the resource gate).

- read  : read-only / pure computation / building workflows (does not change ComfyUI's running state) → allowed
- run   : submit for execution → it may not submit itself; the workflow goes to the orchestrator (a comfy-raw task)
- approve: install nodes, download models, install skills, train LoRAs, ... → needs the user's approval
- deny  : restart / stop ComfyUI, delete models, touch other people's queue, free VRAM, change ComfyUI settings, cloud GPUs, paid APIs → refused
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mcp_client import MCPClient

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CMD = ["node", str(ROOT / "data/tools/comfyui-mcp/node_modules/comfyui-mcp/dist/index.js")]

# (tool, action) → class; action "*" means every action of that tool. Anything unlisted is deny
# (tools added in new versions are not allowed until reviewed).
POLICY: dict[str, dict[str, str]] = {
    "comfy_cli": {
        **dict.fromkeys(
            [
                "status",
                "jobs_list",
                "jobs_status",
                "search_nodes",
                "workflow_validate",
                "models_list_folders",
                "models_list_folder",
                "models_search",
                "models_show",
                "skills_list",
                "skills_show",
                "skills_validate",
                "skills_status",
                "transfer_download",
            ],
            "read",
        ),
        "workflow_run": "run",
        **dict.fromkeys(["models_download", "skills_install", "transfer_upload"], "approve"),
        **dict.fromkeys(
            [
                "server_start",
                "server_stop",
                "server_restart",
                "jobs_wait",
                "jobs_watch",
                "jobs_cancel",
                "models_remove",
                "skills_uninstall",
            ],
            "deny",
        ),
    },
    "enqueue_workflow": {
        "template_schema": "read",
        **dict.fromkeys(["enqueue", "rerun", "run_url", "run_template"], "run"),
    },
    "get_system_stats": {"*": "read"},
    "visualize_workflow": {"*": "read"},
    "create_workflow": {"*": "read"},
    "queue": {
        **dict.fromkeys(["list", "status", "get_workflow"], "read"),
        **dict.fromkeys(["move", "edit", "cancel", "cancel_queued", "clear"], "deny"),
    },
    "search_custom_nodes": {"*": "read"},
    "download_model": {
        **dict.fromkeys(["status", "search", "search_civitai", "search_creators"], "read"),
        **dict.fromkeys(["download", "download_civitai", "resolve_missing", "cancel"], "approve"),
    },
    "list_local_models": {
        **dict.fromkeys(["list", "embeddings", "list_paths"], "read"),
        **dict.fromkeys(["remove", "add_path", "remove_path"], "deny"),
    },
    "get_history": {"*": "read"},
    "get_workflow": {"*": "read"},
    # writes into ComfyUI's workflow library (building), does not affect running
    "save_workflow": {"*": "read"},
    "get_image": {
        **dict.fromkeys(
            [
                "get",
                "view",
                "list_outputs",
                "analyze_color",
                "list_assets",
                "asset_metadata",
                "convert",
            ],
            "read",
        )
    },
    # puts a reference image into ComfyUI input (to prepare a trial run)
    "upload_image": {"*": "read"},
    "get_defaults": {
        **dict.fromkeys(["get", "get_ui"], "read"),
        **dict.fromkeys(["set", "set_ui"], "deny"),
    },
    "generate_image": {"*": "run"},
    "node_snapshot": {"list": "read", "save": "approve", "restore": "deny"},
    "bisect": {"*": "deny"},
    "install_custom_node": {
        "list": "read",
        **dict.fromkeys(
            ["install", "update", "reinstall", "fix", "enable", "disable", "sync_deps"], "approve"
        ),
        "uninstall": "deny",
    },
    "install_comfyui": {"*": "deny"},
    "model_metadata": {**dict.fromkeys(["read", "fetch_civitai"], "read"), "propose": "approve"},
    "workspace": {**dict.fromkeys(["get", "list"], "read"), "set_default": "deny"},
    # paid cloud
    "list_api_nodes": {**dict.fromkeys(["list", "schema"], "read"), "generate": "deny"},
    "node_pack": {
        **dict.fromkeys(["list_files", "read", "search", "verify"], "read"),
        **dict.fromkeys(["scaffold", "write", "patch", "git"], "approve"),
        "publish": "deny",
    },
    "apply_manifest": {"*": "approve"},
    "list_packs": {
        **dict.fromkeys(
            [
                "list",
                "read_workflow",
                "read_manifest",
                "list_templates",
                "check_runtime",
                "extract_deps",
                "skill_list",
                "skill_read",
            ],
            "read",
        ),
        **dict.fromkeys(["install_deps", "generate_skill"], "approve"),
    },
    "calculate": {"*": "read"},
    "train_prepare_dataset": {
        **dict.fromkeys(["list", "detail", "file"], "read"),
        **dict.fromkeys(
            ["prepare", "update", "delete", "caption_image", "caption_dataset"], "approve"
        ),
    },
    "train_start": {
        **dict.fromkeys(["status", "list_flows", "job_config", "preview_config"], "read"),
        **dict.fromkeys(["start", "cancel", "delete"], "approve"),
    },
    "train_doctor": {"doctor": "read", **dict.fromkeys(["bootstrap", "build_image"], "approve")},
    "apps": {
        **dict.fromkeys(["list", "get", "run_status"], "read"),
        "run": "run",
        "import": "approve",
    },
    "batch": {
        **dict.fromkeys(["status", "output"], "read"),
        **dict.fromkeys(["submit", "wait"], "run"),
    },
    "kitchen": {**dict.fromkeys(["status", "assess"], "read"), "apply": "approve"},
    "list_tools": {"*": "read"},
    "describe_tool": {"*": "read"},
    "clear_vram": {"*": "deny"},  # memory is managed by our resource manager
    "restart_comfyui": {"*": "deny"},
    "runpod": {"*": "deny"},
    "runpod_watch": {"*": "deny"},
    "report_issue": {"*": "deny"},
}


def classify(tool: str, args: dict) -> str:
    if tool == "call_tool":  # directory proxy: classify by the tool it actually calls
        inner = args.get("name") or args.get("tool") or ""
        return classify(inner, args.get("args") or args.get("arguments") or {})
    rules = POLICY.get(tool)
    if rules is None:
        return "deny"
    action = args.get("action")
    if "*" in rules:
        return rules["*"]
    return rules.get(action or "", "deny")


def extract_workflow(tool: str, args: dict) -> dict | None:
    """The API-format workflow inside an execution call (handed to the orchestrator to submit); None if there is none."""
    if tool == "call_tool":
        return extract_workflow(args.get("name") or "", args.get("args") or {})
    wf = args.get("workflow") or args.get("prompt") or args.get("graph")
    if isinstance(wf, str):
        try:
            wf = json.loads(wf)
        except json.JSONDecodeError:
            return None
    if (
        isinstance(wf, dict)
        and wf
        and all(isinstance(v, dict) and "class_type" in v for v in wf.values())
    ):
        return wf
    return None


class ComfyBridge:
    def __init__(self, cfg, command: list[str] | None = None):
        self.cfg = cfg
        self.command = command or DEFAULT_CMD
        self.client: MCPClient | None = None
        self._catalog: list[dict] | None = None

    def available(self) -> bool:
        return Path(self.command[-1]).exists()

    def _client(self) -> MCPClient:
        if self.client is None:
            env = {"COMFYUI_URL": self.cfg.comfy_url}
            path = os.path.expanduser("~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI")
            if Path(path).is_dir():
                env["COMFYUI_PATH"] = path
            self.client = MCPClient(self.command, env=env, timeout_s=180)
        return self.client

    def catalog(self) -> list[dict]:
        """Available tools by class (a compact catalogue for the agent)."""
        if self._catalog is None:
            out = []
            for t in self._client().tools():
                acts = (t.get("inputSchema") or {}).get("properties", {}).get("action", {}).get(
                    "enum"
                ) or [None]
                by = {}
                for a in acts:
                    by.setdefault(classify(t["name"], {"action": a} if a else {}), []).append(a)
                out.append(
                    {
                        "tool": t["name"],
                        "summary": (t.get("description") or "").split("\n")[0][:200],
                        "actions": {k: [a for a in v if a] for k, v in by.items()},
                    }
                )
            self._catalog = out
        return self._catalog

    def describe(self, tool: str) -> dict:
        for t in self._client().tools():
            if t["name"] == tool:
                return {
                    "tool": tool,
                    "description": t.get("description"),
                    "schema": t.get("inputSchema"),
                }
        raise KeyError(f"unknown comfy tool {tool}")

    def call(self, tool: str, args: dict) -> dict:
        res = self._client().call(tool, args)
        text = res["text"]
        if len(text) > 12000:
            text = text[:12000] + "\n…(truncated)"
        return {"result": text, "is_error": res["is_error"]}

    def close(self) -> None:
        if self.client:
            self.client.close()
