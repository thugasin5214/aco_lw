# aco_lw · Lightweight local video agent

中文版：[README.zh-CN.md](README.zh-CN.md)

A deterministic orchestrator drives a local ComfyUI (FLUX.2 for images, FreeVideo / MiniMax H3 for video). A local LLM (LM Studio) is an optional planner that wakes on demand. You review candidates in a web console or on Telegram; everything is organised into projects with sessions, a shared project memory and a versioned baseline pack.

- Plan & scope: [`PLAN.md`](PLAN.md) · Design: [`docs/design.html`](docs/design.html) · Measurements: [`docs/baseline.md`](docs/baseline.md)
- Operations: [`docs/runbook.md`](docs/runbook.md) · Progress: [`docs/progress.html`](docs/progress.html) · Architecture: [`docs/architecture.html`](docs/architecture.html)

## Quick start

```sh
uv sync
cp config.example.toml config.toml        # optional: Telegram token, language, ports
uv run va daemon                          # worker + planner + Telegram + console
open http://127.0.0.1:8790                # console (English by default; EN / 中文 switch in the header)
```

Requirements today: Apple Silicon Mac, a running ComfyUI with the templates' models, and (optional) LM Studio. A one-line installer with a setup wizard is planned (phase 5).

## How it works

| Layer | Role |
|---|---|
| Orchestrator | The only component with side effects: validation, memory gate, submit, poll, collect, release, crash-safe recovery |
| Studio | Projects → rounds → tasks; review decisions (approve / redo / tweak / reject / change decision); baseline pack |
| Planner (optional) | Turns natural language into validated parameters; sessions, project memory, prompt-cache-friendly context |
| Channels | Web console and Telegram share the same actions |

## Command line

```sh
uv run va templates                                  # templates, tiers (✓ measured), parameters
uv run va round flux2-t2i -n 4 -p prompt="..."       # queue a round (executed by the daemon)
uv run va status [TASK_ID] · va project list|new|use|done · va pack · va mode
uv run va recover · va release
uv run pytest · uv run ruff check
```

Runtime data (SQLite, per-task manifests and outputs, baseline packs, uploads) lives in `data/` and is not tracked by git.
