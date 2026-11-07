"""通用 MCP：编排器按配置启动 / 连接 MCP 服务，agent 只能请求“列出 / 调用”，由编排器执行。

配置（config.toml，字段与 Claude Code / Codex 的 mcpServers 相同）：

    [mcp_servers.exa]
    url = "https://mcp.exa.ai/mcp"            # Streamable HTTP
    [mcp_servers.fs]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Movies"]
    env = { FOO = "bar" }                     # 只在本进程里用，不进日志、不交给 LLM

审批（policies.yaml 的 `mcp`）：默认只读工具（annotations.readOnlyHint）直接跑，其他每次经你批准；
可按服务覆盖。ComfyUI 的 MCP 不在这里——它走 comfy 工具（分类 + 生成只经编排器）。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from .mcp_client import MCPClient, MCPError

log = logging.getLogger("aco_lw.mcp")
PROTOCOL = "2025-06-18"


class HTTPMCPClient:
    """Streamable HTTP 传输：POST JSON-RPC，回复可能是 JSON 或 SSE。"""

    def __init__(self, url: str, headers: dict | None = None, timeout_s: float = 120):
        self.url, self.headers, self.timeout_s = url, dict(headers or {}), timeout_s
        self.session: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: dict = {}
        self._started = False

    def _post(self, msg: dict) -> Any:
        import httpx

        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL,
            **self.headers,
        }
        if self.session:
            h["Mcp-Session-Id"] = self.session
        r = httpx.post(self.url, json=msg, headers=h, timeout=self.timeout_s)
        r.raise_for_status()
        self.session = r.headers.get("mcp-session-id") or self.session
        if "id" not in msg:
            return None
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if data.get("id") == msg["id"]:
                    return self._result(data)
            raise MCPError("no response in event stream")
        return self._result(r.json())

    @staticmethod
    def _result(data: dict) -> Any:
        if "error" in data:
            raise MCPError(str(data["error"].get("message") or data["error"])[:500])
        return data.get("result")

    def request(self, method: str, params: dict) -> Any:
        with self._lock:
            self._id += 1
            return self._post(
                {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            )

    def start(self) -> None:
        if self._started:
            return
        out = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "aco_lw", "version": "0.1"},
            },
        )
        self.server_info = (out or {}).get("serverInfo", {})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._started = True

    def close(self) -> None:
        self._started, self.session = False, None

    tools = MCPClient.tools
    call = MCPClient.call


class MCPHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clients: dict[str, Any] = {}
        self._tools: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def config(self) -> dict[str, dict]:
        servers = getattr(self.cfg, "mcp_servers", {}) or {}
        return {
            k: v for k, v in servers.items() if not v.get("disabled") and v.get("enabled", True)
        }

    def _client(self, name: str):
        conf = self.config().get(name)
        if conf is None:
            raise KeyError(
                f"unknown MCP server {name}; configured: {', '.join(self.config()) or 'none'}"
            )
        with self._lock:
            if name not in self.clients:
                timeout = float(conf.get("timeout_s", 300))
                if conf.get("url"):
                    self.clients[name] = HTTPMCPClient(conf["url"], conf.get("headers"), timeout)
                else:
                    cmd = [conf["command"], *conf.get("args", [])]
                    self.clients[name] = MCPClient(
                        cmd, env=conf.get("env"), cwd=conf.get("cwd"), timeout_s=timeout
                    )
            return self.clients[name]

    def tools(self, name: str) -> list[dict]:
        if name not in self._tools:
            self._tools[name] = self._client(name).tools()
        return self._tools[name]

    def catalog(self, server: str | None = None) -> dict:
        """不带 server：每个服务的工具名 + 一句话；带 server：该服务工具的完整参数 schema。"""
        if server:
            return {
                "server": server,
                "tools": [
                    {
                        "name": t["name"],
                        "description": (t.get("description") or "")[:1500],
                        "input_schema": t.get("inputSchema") or {},
                        "read_only": bool((t.get("annotations") or {}).get("readOnlyHint")),
                    }
                    for t in self.tools(server)
                ],
            }
        out: dict = {}
        for name in self.config():
            try:
                out[name] = [
                    f"{t['name']}: {(t.get('description') or '').splitlines()[0][:160] if t.get('description') else ''}"
                    for t in self.tools(name)
                ]
            except Exception as e:  # noqa: BLE001 — 一个服务连不上不影响其他
                out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
        return {"servers": out}

    def read_only(self, server: str, tool: str) -> bool:
        try:
            t = next(t for t in self.tools(server) if t["name"] == tool)
        except StopIteration:
            raise KeyError(f"MCP server {server} has no tool {tool}") from None
        return bool((t.get("annotations") or {}).get("readOnlyHint"))

    def call(self, server: str, tool: str, args: dict) -> dict:
        out = self._client(server).call(tool, args or {})
        return {"server": server, "tool": tool, **out}

    def close(self) -> None:
        for c in self.clients.values():
            try:
                c.close()
            except Exception:
                log.warning("closing MCP client failed", exc_info=True)
        self.clients.clear()
