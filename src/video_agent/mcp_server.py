"""aco_lw 的 MCP 服务（stdio）：给 Claude Code / Codex 用，只暴露 aco_lw 的工具。

每个工具调用都转发给 daemon（POST /api/agent/tool），由 daemon 里的 agent 运行时执行——
执行策略、审批、资源调度与本地 LLM 完全一致；这个进程本身不做任何事。

    python -m video_agent.mcp_server --run RUN_ID --url http://127.0.0.1:8790
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

import httpx

PROTOCOL = "2025-06-18"


class Server:
    def __init__(self, run_id: str, url: str, http: Callable | None = None, timeout: float = 3600):
        self.run_id, self.url, self.timeout = run_id, url.rstrip("/"), timeout
        self.http = http or httpx.Client(timeout=timeout)

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # 通知（如 notifications/initialized）不回复
            return None
        try:
            if method == "initialize":
                ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
                result = {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aco_lw", "version": "0.1"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = self.http.get(f"{self.url}/api/agent/tools").json()
                result = {
                    "tools": [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": t["parameters"],
                        }
                        for t in tools
                    ]
                }
            elif method == "tools/call":
                p = msg.get("params") or {}
                r = self.http.post(
                    f"{self.url}/api/agent/tool",
                    json={
                        "run_id": self.run_id,
                        "name": p.get("name"),
                        "arguments": p.get("arguments") or {},
                    },
                    timeout=self.timeout,
                )
                out = r.json()
                result = {
                    "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                    "isError": isinstance(out, dict) and "error" in out,
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
        except (httpx.HTTPError, ValueError) as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            reply = self.handle(json.loads(line))
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8790")
    ap.add_argument("--timeout", type=float, default=3600)
    a = ap.parse_args()
    Server(a.run, a.url, timeout=a.timeout).serve()


if __name__ == "__main__":
    main()
