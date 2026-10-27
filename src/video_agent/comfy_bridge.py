"""第三方 ComfyUI agent 工具（artokun/comfyui-mcp）的桥：由编排器启动、按“工具 + 动作”分类放行。

用户 2026-10-27 决定：agent 可以探索、搭建、试跑；安装节点 / 下载模型要用户批准；
执行类调用由我们的编排器调度（成为可追踪的任务，受暂停与资源闸门）。

- read  ：只读 / 纯计算 / 搭建 workflow（不改 ComfyUI 运行状态）→ 放行
- run   ：提交执行 → 不让它自己提交；workflow 交给编排器（comfy-raw 任务）
- approve：安装节点、下载模型、装 skill、训练 LoRA 等 → 需要用户批准
- deny  ：重启 / 停止 ComfyUI、删模型、动别人的队列、释放显存、改 ComfyUI 设置、云 GPU、付费 API → 拒绝
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mcp_client import MCPClient

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CMD = ["node", str(ROOT / "data/tools/comfyui-mcp/node_modules/comfyui-mcp/dist/index.js")]

# (tool, action) → 类别；action 为 "*" 表示该工具所有动作。未列出的一律 deny（新版本新增的工具先不放行）。
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
    "save_workflow": {"*": "read"},  # 写进 ComfyUI 的 workflow 库（搭建），不影响运行
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
    "upload_image": {"*": "read"},  # 把参考图放到 ComfyUI input（为试跑准备）
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
    "list_api_nodes": {**dict.fromkeys(["list", "schema"], "read"), "generate": "deny"},  # 付费云端
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
    "clear_vram": {"*": "deny"},  # 内存由我们的资源管理器管
    "restart_comfyui": {"*": "deny"},
    "runpod": {"*": "deny"},
    "runpod_watch": {"*": "deny"},
    "report_issue": {"*": "deny"},
}


def classify(tool: str, args: dict) -> str:
    if tool == "call_tool":  # 目录代理：按里面真正要调用的工具分类
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
    """执行类调用里的 API 格式 workflow（交给编排器提交）；拿不到就返回 None。"""
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
        """按类别列出可用的工具（给 agent 看的精简目录）。"""
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
