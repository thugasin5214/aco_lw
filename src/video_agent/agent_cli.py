"""把一次请求交给本机的 agent CLI（Claude Code / Codex，用它们自己的在线模型与登录）。

它们只拿到 aco_lw 的 MCP 工具：Claude Code 用 `--tools ""` 关掉全部内置工具（shell、读写文件、联网），
Codex 用只读沙箱。所有工具调用经 daemon 执行，执行策略、审批、资源调度不变。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


class AgentCLIError(RuntimeError):
    pass


class AgentCLI:
    def __init__(self, name: str, cfg):
        if name not in ("claude_code", "codex"):
            raise ValueError(f"unknown agent CLI {name}")
        self.name, self.cfg = name, cfg

    def mcp_server(self, run_id: str, url: str) -> dict:
        return {
            "command": sys.executable,
            "args": [
                "-m",
                "video_agent.mcp_server",
                "--run",
                run_id,
                "--url",
                url,
                "--timeout",
                str(self.cfg.agent_cli_timeout_s),
            ],
        }

    def command(self, run_id: str, url: str, workdir: Path) -> list[str]:
        spec = self.mcp_server(run_id, url)
        if self.name == "claude_code":
            cfg_file = workdir / "mcp.json"
            cfg_file.write_text(json.dumps({"mcpServers": {"aco_lw": spec}}))
            cmd = [
                self.cfg.claude_cmd,
                "-p",
                "--output-format",
                "json",
                "--tools",
                "",  # 关掉全部内置工具
                "--strict-mcp-config",
                "--mcp-config",
                str(cfg_file),
                "--allowedTools",
                "mcp__aco_lw",
            ]  # 只自动批准 aco_lw 的工具
            if self.cfg.claude_model:
                cmd += ["--model", self.cfg.claude_model]
            return cmd
        cmd = [
            self.cfg.codex_cmd,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "-c",
            f"mcp_servers.aco_lw.command={json.dumps(spec['command'])}",
            "-c",
            f"mcp_servers.aco_lw.args={json.dumps(spec['args'])}",
            "-c",
            'approval_policy="never"',
            "-o",
            str(workdir / "last_message.txt"),
        ]
        if self.cfg.codex_model:
            cmd += ["-m", self.cfg.codex_model]
        return [*cmd, "-"]  # 指令从 stdin 读

    def run(self, run_id: str, prompt: str, url: str) -> tuple[str, dict]:
        workdir = self.cfg.data_dir / "agent_cli" / run_id  # 空目录：不给它看任何项目文件
        workdir.mkdir(parents=True, exist_ok=True)
        t = time.monotonic()
        proc = subprocess.run(
            self.command(run_id, url, workdir),
            input=prompt,
            text=True,
            capture_output=True,
            cwd=workdir,
            check=False,
            timeout=self.cfg.agent_cli_timeout_s,
        )
        stats = {"seconds": round(time.monotonic() - t, 1), "via": self.name}
        if self.name == "claude_code":
            try:
                obj = json.loads(proc.stdout)
            except json.JSONDecodeError:
                raise AgentCLIError(
                    f"claude exited {proc.returncode}: {proc.stderr[-500:]}"
                ) from None
            if obj.get("is_error"):
                raise AgentCLIError(str(obj.get("result") or obj)[:500])
            stats.update(cost_usd=obj.get("total_cost_usd"), turns=obj.get("num_turns"))
            return obj.get("result") or "", stats
        last = workdir / "last_message.txt"
        if proc.returncode != 0 and not last.exists():
            raise AgentCLIError(f"codex exited {proc.returncode}: {proc.stderr[-500:]}")
        return (last.read_text().strip() if last.exists() else ""), stats
