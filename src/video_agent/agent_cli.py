"""Hand one request to a local agent CLI (Claude Code / Codex, with their own online models and logins).

They only get aco_lw's MCP tools: Claude Code uses `--tools ""` to turn off every built-in tool (shell, file read/write, web),
Codex uses a read-only sandbox. Every tool call is executed by the daemon; execution policy, approvals and resource scheduling are unchanged.

`freevideo`: hand off to FreeVideo's agent — i.e. Claude Code plus FreeVideo's own MCP server (config taken from
FreeVideo's /freevideo/agent/connect, written only into this run's --mcp-config, your global Claude Code config is untouched;
the token is read by FreeVideo's MCP server itself). What it submits sits in the FreeVideo queue, so aco's resource gate still sees it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class AgentCLIError(RuntimeError):
    pass


class AgentCLI:
    def __init__(self, name: str, cfg, extra_mcp: dict | None = None):
        if name not in ("claude_code", "codex", "freevideo"):
            raise ValueError(f"unknown agent CLI {name}")
        self.name, self.cfg = name, cfg
        self.extra_mcp = extra_mcp or {}  # extra MCP servers {name: launch config}

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
        if self.name in ("claude_code", "freevideo"):
            cfg_file = workdir / f"{run_id}.mcp.json"
            cfg_file.write_text(json.dumps({"mcpServers": {"aco_lw": spec, **self.extra_mcp}}))
            allowed = " ".join(["mcp__aco_lw", *(f"mcp__{k}" for k in self.extra_mcp)])
            cmd = [
                self.cfg.claude_cmd,
                "-p",
                "--output-format",
                "json",
                "--tools",
                "",  # turn off all built-in tools
                "--strict-mcp-config",
                "--mcp-config",
                str(cfg_file),
                "--allowedTools",
                allowed,  # auto-approve only these MCP servers' tools
                "--no-session-persistence",  # do not write into your Claude Code session history
            ]
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
            str(workdir / f"{run_id}.last_message.txt"),
        ]
        if self.cfg.codex_model:
            cmd += ["-m", self.cfg.codex_model]
        return [*cmd, "-"]  # instructions come from stdin

    def run(self, run_id: str, prompt: str, url: str) -> tuple[str, dict]:
        # an empty temp directory outside the repo: the CLI sees no project files or git info
        # (measured: inside the repo it picks up commit history)
        # one fixed directory (not a new one each time, so the CLI does not keep a memory per
        # directory); each run gets a sub-file holding only its config
        workdir = Path(tempfile.gettempdir()) / "aco_lw_agent_cli"
        workdir.mkdir(exist_ok=True)
        t = time.monotonic()
        proc = subprocess.run(
            self.command(run_id, url, workdir),
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=workdir,
            check=False,
            timeout=self.cfg.agent_cli_timeout_s,
        )
        stats = {"seconds": round(time.monotonic() - t, 1), "via": self.name}
        if self.name in ("claude_code", "freevideo"):
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
        last = workdir / f"{run_id}.last_message.txt"
        error = self._codex_error(proc.stdout)
        if error or (proc.returncode != 0 and not last.exists()):
            raise AgentCLIError(
                f"codex: {error or proc.stderr[-500:] or f'exit {proc.returncode}'}"
            )
        return (last.read_text().strip() if last.exists() else ""), stats

    @staticmethod
    def _codex_error(stdout: str) -> str | None:
        """codex exec --json reports errors in the event stream (turn.failed / error), not on stderr."""
        for line in reversed(stdout.splitlines()):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") in ("turn.failed", "error"):
                msg = (
                    (ev.get("error") or {}).get("message") if ev.get("error") else ev.get("message")
                )
                try:  # the inner part is often JSON again
                    inner = json.loads(msg)
                    msg = (inner.get("error") or {}).get("message") or msg
                except (TypeError, json.JSONDecodeError):
                    pass
                return str(msg)[:500]
        return None
