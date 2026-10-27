# aco_lw Runbook

Updated 2026-10-26 · applies to phase 2.5 + W2 (`va daemon` + console + Telegram + sessions & project memory) · 中文版：[runbook.zh-CN.md](runbook.zh-CN.md)

This runbook answers three questions: **how to start / stop**, **how to use it day to day**, and **what to check when something goes wrong**. Design rationale is in `docs/design.html`, plan and progress in `PLAN.md` / `docs/progress.html`, measurements in `docs/baseline.md`.

---

## 1. Components and locations

| Component | Address / location | Notes |
|---|---|---|
| aco_lw console | http://127.0.0.1:8790 | Started by `va daemon`; local only. **Port 8765 belongs to another server — don't use it** |
| ComfyUI (FreeVideo-local) | http://127.0.0.1:8188 · `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` | Executor: FLUX.2 images, FreeVideo / MiniMax H3 video. aco_lw does not start it |
| LM Studio | http://127.0.0.1:1234 · CLI `lms` | 27B planner (`qwen3.8-27b-splash`), loaded on demand, unloaded when idle |
| Telegram bot | @luwu_abot | Whitelisted chat_ids live in `config.toml` |
| Code | `~/workplace/video-agent` (private GitHub repo `aco_lw`) | |
| Config | `config.toml` (git-ignored, mode 600) | Secrets such as the bot token live only here |
| Planner rules | `prompts/planner_rules.md` | Editable; changing it invalidates the LLM prefix cache once |
| Data | `data/` | `templates/` (promoted templates), `variants/` (imported workflows), `tasks.sqlite` (index), `jobs/<task>/` (manifest + outputs), `packs/<project>/v<N>/` (baseline pack), `uploads/<project>/` (reference images), `daemon.log` |

## 2. Start and stop

### Start order
1. **ComfyUI** — make sure http://127.0.0.1:8188 opens (FreeVideo-local environment).
2. **LM Studio** — the app just needs to be running; aco_lw loads the model when needed.
3. **aco_lw** — run:

```sh
cd ~/workplace/video-agent
uv run va daemon 2>&1 | tee -a data/daemon.log
```

Ready when you see `Telegram listener started (whitelist [...])` and `Console: http://127.0.0.1:8790`. Startup reconciles unfinished tasks (`recover`): it only collects finished results and **never resubmits**.

### Stop
- `Ctrl-C` in the daemon terminal, or `pkill -f "va daemon"`.
- A job already running inside ComfyUI is **not cancelled**; the next daemon start reconciles it by prompt_id and collects its outputs.

### Console only
`uv run va serve` opens the console (browse + review) without rendering; queued tasks run once the daemon is up.

## 3. Daily use

### 3.1 A full creative loop
1. **Ask** — in the console (*Projects & Chat*) or in Telegram, in English or Chinese. Example: “Same blonde academy girl, half-body, looking back at the camera, warm evening window light.” You can **attach a reference image** (📎 in the console, or send a photo in Telegram) and say “use this face”.
   The planner acknowledges immediately, understands the request (typically 10–20 s; the first message after a cold start adds ~15 s for loading) and queues a round (4 images by default, 1 for video).
2. **Wait** — images take ~12 s each; a 640×384×3 s video takes ~5–6 min. When done, the *Review* tab shows a badge and Telegram receives the original files with buttons.
3. **Review** (console `#review` or Telegram buttons — equivalent):

   | Action | Effect |
   |---|---|
   | ✅ Approve | Select this candidate (others are marked rejected); optionally **lock it into the baseline pack** as character / scene / style |
   | 🎲 Redo round | Discard the round; same parameters, new seeds |
   | 🔧 Tweak with note | Keep #N's seed; the LLM rewrites the prompt from your note; new round |
   | ↩️ Reject + note | The LLM adjusts parameters from your note; new round (empty note = reject only) |
   | ✏️ Manual tweak | Edit the parameters yourself in a form (prompt, seed, steps, reference, tier, count); no LLM |
   | ↺ Change decision | Reopen a decided round; the old decision is kept in history and any pack lock it made is undone in a new pack version |

   You can also just type: “#2 is good, lock it as the character”, or “she looks too young” — when a round is waiting for review, feedback typed in chat is applied to that round and the new round is linked to it in the DAG.
   Console shortcuts: `1–9` highlight · `Enter` approve · `F` tweak with note · `R` redo · `X` reject · `J/K` switch item.
4. **Carry on from locked assets** — once a character is locked, say “use this character …”; the planner references `@pack:character` (reference edit / image-to-video).

### 3.2 Projects, sessions and project memory
- **Project** = one creative goal. Rounds, tasks, the baseline pack, sessions and memory all belong to it. Completed projects stay browsable forever (*Projects & Chat*, filter “Completed”).
- **Session** = one conversation thread inside a project. Start a new one when switching topic (`+ New session` or `/session new`); continue an old one any time. Older messages from before sessions existed are in the “History” session.
- **Project memory** = durable facts shared by all sessions of the project (e.g. “camera stays at 35mm”, “about 19, refined CG-leaning-photoreal”). The LLM **proposes** entries after relevant messages; they only take effect after you press **Remember** (console *Project memory* tab or Telegram button). You can add, edit (creates a new version), pin, archive and restore entries.
- **Long sessions** are summarized automatically: older exchanges are folded into a session summary; the original messages stay in history.
- **View LLM context** (console, per session) shows exactly what the planner will receive next — useful when it misunderstands.

### 3.3 Commands (Telegram and console chat; never wake the LLM)

| Command | Purpose |
|---|---|
| `/status` | Project, rendering state, queue, memory, LLM mode |
| `/review` | Re-send rounds waiting for review to Telegram |
| `/round TEMPLATE [COUNT] [prompt]` | Queue a round without the LLM (`flux2-t2i` `flux2-edit` `h3-t2v` `h3-i2v`) |
| `/redo` | Redo the latest round under review |
| `/reopen [ROUND_ID]` | Change the decision of the latest (or given) decided round |
| `/pause` `/resume` | Pause / resume rendering (the running task finishes) |
| `/cancel` | Cancel this project's queued tasks and interrupt our running task |
| `/mode [on_demand\|session\|always\|command_only]` | LLM residency (default on demand) |
| `/project` · `/projects` · `/project new NAME` · `/project use NAME` · `/project done` | Projects |
| `/session` · `/session new [TITLE]` · `/session use N` | Sessions (N: 1 = most recent) |
| `/memory` · `/memory add TEXT` · `/memory del ID` | Project memory |
| `/pack` | Current baseline pack |
| `/whoami` | Show your chat_id (works for anyone; used to set up the whitelist) |

### 3.4 Command line (local)

```sh
uv run va templates                     # templates, tiers (✓ measured), parameters
uv run va round flux2-t2i -n 4 -p prompt="..."   # queue a round (run by the daemon)
uv run va status [TASK_ID]               # task list / one manifest
uv run va project list|new|use|done      # projects
uv run va pack                           # baseline pack versions
uv run va mode [MODE]                    # LLM residency
uv run va recover                        # manual reconcile (the daemon does this on start)
uv run va release                        # free ComfyUI models when its queue is idle
```

### 3.5 Language
The console defaults to **English**; switch with the EN / 中文 selector in the header (remembered per browser). System messages and Telegram texts follow `language` in `config.toml` (`en` default, `zh` for Chinese). The planner replies in the language you write in.

### 3.6 Edit a workflow in ComfyUI (advanced)
For changes the parameters can't express — new nodes, different wiring, another model, a LoRA:
1. Open a task (Flow → node → candidate) and click **⬇ Export workflow**. You get the exact API workflow that task ran.
2. Click **Open ComfyUI ↗**, drag the file onto the canvas (or Ctrl+O). Node IDs are preserved.
3. Edit it, then in ComfyUI choose **Workflow → Export (API)**.
4. Back on the same task, click **⬆ Import edited workflow** and pick that file. aco_lw shows only *your* changes (diff against what you exported), which template slots still map, and checks every node, value and link against ComfyUI's `/object_info`. Problems → status *Invalid*.
5. In *Projects & Chat → Workflows*: **▶ Run trial** renders one candidate at the lowest tier using the values from your edited graph (only the size comes from the tier).
6. If the trial passes, **⭐ Promote** with a name (lowercase, digits, dashes). It becomes a normal template for all projects, stored in `data/templates/`; only the tier you trialled is marked as measured. **✕ Reject** discards it and cancels its queued tasks.

Candidates are never picked by the planner; you can run them manually (`/round <variant-name>`, manual tweak) until promoted.

### 3.7 Online models and local agents (fallback)
When the local LLM can't run — most often because a video render holds the memory — requests normally wait. You can let them continue online instead, in `config.toml`:

```toml
allow_online = true                                   # privacy switch, off by default
llm_fallback = ["anthropic", "claude_code", "codex"]  # tried in order
```

- `anthropic`, `openrouter` (or any `openai_compatible` endpoint): the online API continues the same request step by step. Keys: `ANTHROPIC_API_KEY` / `ant auth login` or `anthropic_api_key`; `OPENROUTER_API_KEY` or `openrouter_api_key` (plus `openrouter_model`).
- `claude_code` / `codex`: the local CLI, with its own login and online model, runs the whole request. It gets **only aco_lw's tools** over MCP (Claude Code with all built-in tools disabled, Codex in a read-only sandbox); every tool call still goes through the execution policy, approvals and the scheduler.
- Without `allow_online = true` nothing is sent out, and the console shows the request waiting for resources with the reason.
- Chat bubbles show which backend answered ("via …") and, for Claude Code, the cost.

## 4. Resources and memory policy (automatic)

| Situation | What the system does |
|---|---|
| Before submitting a video | **Unload the 27B** (video peaks at ~55 GiB; coexisting triggers memory-pressure warnings); requires ≥ 62 GiB free |
| After a video | `/free` if the ComfyUI queue is idle; reload the 27B and **prewarm** the current session's prompt cache |
| Image tasks | Keep the 27B and the image model loaded (warm 11 s vs cold 23 s) |
| Not enough memory | `/free` (idle queue only) → unload LLM → wait up to 10 min → time out without submitting |
| Someone else's ComfyUI job | Wait for an idle queue; never pre-empt or cancel it |
| Free text while a video renders | Queued for the LLM until the video finishes; commands still respond immediately |
| LLM idle | Unloaded after 20 min while a session is active (keeps the prompt cache warm), otherwise per TTL |

**Prompt caching.** LM Studio reuses the KV cache only when a new request *extends* an earlier one (measured: 0.3–0.5 s vs ~48 s cold for 17k tokens). The planner therefore replays each session's earlier exchanges verbatim and puts volatile state last; typical multi-turn reuse is 55–75 %. Editing project memory, the rules file, or a summary rewrite causes one full recompute.

## 5. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Header shows “daemon not running” | `pgrep -fl "va daemon"` | Start it (§2); queued tasks continue |
| “ComfyUI offline” | Open http://127.0.0.1:8188 | Start FreeVideo-local's ComfyUI; aco_lw won't start it |
| Task stuck in “waiting memory” | Header memory / pressure; ComfyUI queue | Close memory-heavy apps or `/cancel`; after 10 min it times out without submitting |
| Task “timed out” | `va status <ID>` for the prompt_id | It may still be running: `va recover` collects it; don't resubmit by hand |
| Task “failed” | Error in the console details / ComfyUI terminal | Adjust and redo; failed tasks may be resubmitted with the same parameters |
| Telegram silent | `grep "Ignoring message from non-whitelisted" data/daemon.log` | Send `/whoami`, add the number to `telegram_chat_ids` in `config.toml`, restart |
| Telegram send failures | `grep "Telegram send" data/daemon.log` | Network or token problem; if the token leaked, `/revoke` in @BotFather, update `config.toml`, restart |
| Planner slow | LLM bubble shows seconds, tokens and cache reuse | Normal: 10–20 s, mostly “thinking”. `planner_fast = true` in `config.toml` cuts ~35 % at some cost in understanding |
| Planner misunderstood | *View LLM context*; project memory | Use the round buttons (tweak / reject + note) for precise feedback; correct project memory; or `/round` with an explicit prompt |
| “Another orchestrator is running” | — | Don't use `va submit` while the daemon runs; use `va round` to queue |

## 6. Data, backup and cleanup
- **Back up** `data/tasks.sqlite`, `data/jobs/`, `data/packs/`, `data/uploads/`, `config.toml`. Safest with the daemon stopped (SQLite uses WAL; while running, copy the `-wal` file too).
- **Outputs**: save from the console, or `data/jobs/<TASK_ID>/`. ComfyUI's originals are under `FreeVideo-local/ComfyUI/output/` (`aco_lw/<TASK_ID>` prefix or `FreeVideo/<date>/`).
- **Cleanup**: stop the daemon, then delete `data/jobs/<ID>` folders you no longer need (database rows remain; the console will show missing images).

## 7. Safety constraints (do not change)
- Only the orchestrator talks to ComfyUI; the LLM and Telegram only produce data or call fixed commands.
- Never cancel or free other people's jobs; `/free` double-checks that the queue is empty.
- Don't change ComfyUI launch options or global LM Studio / OpenClaw settings; don't download models or install custom nodes.
- Telegram answers whitelisted chats only; the console binds to 127.0.0.1.
- Project memory written by the LLM needs your confirmation.

## 8. Known issues (2026-10-26)
1. The planner occasionally misreads ambiguous words (“too small” meaning age vs framing). Specific notes work best; *View LLM context* shows what it saw.
2. Planner replies take 10–20 s, mostly model “thinking” that the API can't switch off for this model (`/no_think` halves it; see `planner_fast`).
