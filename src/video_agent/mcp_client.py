"""最小的 MCP 客户端（stdio JSON-RPC）：由编排器启动第三方 MCP 服务（例如 comfyui-mcp），
列出并调用它的工具。调用一律经 comfy_tools.py 的分类与策略，不直接暴露给 LLM。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(
        self,
        command: list[str],
        env: dict | None = None,
        cwd: str | None = None,
        timeout_s: float = 120,
    ):
        self.command, self.env, self.cwd, self.timeout_s = command, env or {}, cwd, timeout_s
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: dict = {}

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **self.env},
            cwd=self.cwd,
            text=True,
            bufsize=1,
        )
        out = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aco_lw", "version": "0.1"},
            },
        )
        self.server_info = out.get("serverInfo", {})
        self.notify("notifications/initialized", {})

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            assert self.proc and self.proc.stdout
            timer = threading.Timer(self.timeout_s, self.proc.kill)
            timer.start()
            try:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        raise MCPError(f"MCP server exited while waiting for {method}")
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 服务把日志打到了 stdout：跳过
                    if msg.get("id") != rid:
                        continue  # 服务端通知 / 别的回复
                    if "error" in msg:
                        raise MCPError(str(msg["error"].get("message") or msg["error"])[:500])
                    return msg.get("result")
            finally:
                timer.cancel()

    def tools(self) -> list[dict]:
        self.start()
        out, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            out += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def call(self, name: str, arguments: dict) -> dict:
        self.start()
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"
        )
        return {
            "text": text,
            "is_error": bool(res.get("isError")),
            "structured": res.get("structuredContent"),
        }
