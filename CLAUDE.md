# video-agent

Local video agent. Plan and progress: `PLAN.md`; design rationale: `docs/design.html`.

## Hard rules
- The orchestrator is the hub: the agent (LLM) only proposes tool calls; the orchestrator (`src/video_agent/`) executes them and hands the results back when it can (results are parked while the LLM is unloaded). Every ComfyUI / FreeVideo side effect goes only through the orchestrator's generation tools. Generic tools (reading web pages, running commands, reading/writing files, skills, agent memory, MCP servers; see `toolbox.py` / `mcp_hub.py`) are also executed by the orchestrator. Actions that need approval (commands, non-read-only MCP, delegating to an external agent, unmeasured video tiers) need the user's approval for each item by default; the user can press "Always allow" to store a rule, or skip all approvals with `/permissions bypass` (`run_command.auto_allow` in `policies.yaml` can also allow single-command prefixes). In every mode, refuse: direct access to :8188, reading the FreeVideo token, killing processes, loading/unloading the LLM. Installing nodes / downloading models still asks every time under bypass (user decision 2026-11-07).
- Exception: `/fv` and `delegate_freevideo` (each approved by the user every time) hand the whole task to the FreeVideo agent — Claude Code + FreeVideo's own MCP, which generates directly inside FreeVideo (not as aco tasks / rounds; aco's resource gate still sees the FreeVideo queue). The MCP config comes from FreeVideo's `/freevideo/agent/connect` and is written only into that run's `--mcp-config`; the global Claude Code config is never changed. Online use is allowed only by `freevideo_agent_online`.
- Never cancel or release tasks / models not submitted by this system; refuse `/free` while the queue is not empty.
- Never change ComfyUI's startup parameters or the global OpenClaw / LM Studio config.
- The agent may explore ComfyUI (models, nodes, workflows), build / modify workflows and trial-run at the lowest tier (all through the orchestrator); **downloading models and installing custom nodes need the user's approval first** (user decision 2026-10-27).
- The FreeVideo agent API token (`FreeVideo-local/agent/token`) is read only in-process; never print it or write it into any other file.
- Get the user's confirmation before submitting real generation tasks (video especially: FreeVideo takes memory adaptively and one run takes minutes).
- Label numbers as measured or estimated (实测 / 估算). All runtime data goes in `data/` (gitignored).

## Conventions
- Code comments and docstrings are in English. User-facing text (chat replies, Telegram, console) goes through `i18n.py` (en / zh); don't hard-code it.
- `prompts/planner_rules.md` is a prompt for the local LLM, not documentation: its language affects model behaviour, so test with real requests before changing it.

## Environment
- ComfyUI: http://127.0.0.1:8188, actually `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` (video = FreeVideo / MiniMax H3); `~/workplace/localvideo` is the old environment, don't use it
- LM Studio: http://127.0.0.1:1234, CLI `lms`
- Source API templates: `~/Documents/ChatGPT/jev/workflows/标准实验室/*-API.json`
- Commands: `uv run pytest`, `uv run ruff check`, `uv run va ...`
- The web console's default port is 8790 (8765 is used by another server of the user's; don't take it)
