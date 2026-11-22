# aco_lw Runbook

Updated 2026-11-07 · applies to `va daemon` + console + Telegram + the general agent (tools, vision, MCP, online brains, permission modes) · 中文版：[runbook.zh-CN.md](runbook.zh-CN.md)

This runbook answers three questions: **how to start / stop**, **how to use it day to day**, and **what to check when something goes wrong**. Progress is in `docs/progress.html`, architecture diagrams and flow charts in `docs/architecture.html` (console → Architecture).

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
| Planner rules | `prompts/planner_rules.md` · `prompts/agent_rules.md` | Editable; changing it invalidates the LLM prefix cache once |
| Execution policy | `policies.yaml` | Per-tool allow / approval / limits; MCP approval rules; `run_command.auto_allow` |
| Agent workspace and memory | `data/agent-work/<run>/` · `data/agent_memory/` | Commands run in the workspace; memory is the agent's own notes (`MEMORY.md` is the index) |
| Data | `data/` | `templates/` (promoted templates), `variants/` (imported workflows), `tasks.sqlite` (index), `jobs/<task>/` (manifest + outputs), `packs/<project>/v<N>/` (baseline pack), `uploads/<project>/` (reference images), `daemon.log` |

## 2. Start and stop

### Three services

| Service | Address | Role |
|---|---|---|
| ComfyUI (with the FreeVideo plugin) | http://127.0.0.1:8188 | Renders images and video. FreeVideo is not a separate program: it is a ComfyUI plugin and is there whenever ComfyUI is up |
| LM Studio | http://127.0.0.1:1234 | Local LLM (aco's brain) |
| aco (`va daemon`) | http://127.0.0.1:8790 | Orchestrator + console + Telegram |

Start them in any order: if aco comes up first it retries ComfyUI every 30 s, and queued tasks wait until it connects.

### 1) Start ComfyUI + FreeVideo
FreeVideo.app is not installed on this machine; ComfyUI is started directly with FreeVideo-local's own Python (same arguments as the FreeVideo launcher; don't change them).

Foreground (keep a terminal window for it; `Ctrl-C` stops it):

```sh
cd ~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI
../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188
```

Background (keeps running after the terminal closes):

```sh
cd ~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI
nohup ../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188 >/dev/null 2>&1 &
```

- Ready in about 10–20 s (measured 18 s, together with aco). Check: `curl -s http://127.0.0.1:8188/system_stats` returns JSON, or open http://127.0.0.1:8188 in a browser.
- FreeVideo panel: open http://127.0.0.1:8188 → **Workflow → Browse templates → FreeVideo → FreeVideo-All-in-One**; the FreeVideo agent chat is under **Extensions → FreeVideo**.
- Log: the FreeVideo plugin writes ComfyUI's output to `~/Documents/ChatGPT/jev/FreeVideo-local/launcher/comfy-runs/connected-<time>-<pid>/comfy.log` (the newest folder is the current run).
- Stop: `Ctrl-C` in the foreground; `pkill -f "ComfyUI/main.py"` in the background. Make sure no video is rendering first, or it is cut off.
- The `envs/comfyui-<hash>` folder name may change after a FreeVideo upgrade; the `python` entry in `FreeVideo-local/launcher/comfy-host.json` is authoritative.

### 2) Start LM Studio
Open the LM Studio app (or `lms server start` in a terminal). No need to load a model: aco loads it when needed and unloads it when idle. Check: `curl -s http://127.0.0.1:1234/v1/models`.

### 3) Start aco
Foreground (`Ctrl-C` stops it; the log also goes to `data/daemon.log`):

```sh
cd ~/workplace/video-agent
uv run va daemon 2>&1 | tee -a data/daemon.log
```

Background:

```sh
cd ~/workplace/video-agent
nohup uv run va daemon --port 8790 >> data/daemon.log 2>&1 &
```

- Ready when the log shows `Telegram listener started (whitelist [...])` and `Console: http://127.0.0.1:8790`. Check: `curl -s http://127.0.0.1:8790/api/system` shows `comfy.online: true` and `daemon.alive: true`.
- Console: open http://127.0.0.1:8790; on Telegram just message the bot.
- Startup reconciles unfinished tasks (`recover`): it only collects finished results and **never resubmits**. With ComfyUI down the log shows `Startup reconcile failed (ComfyUI offline?)`; once ComfyUI is up it carries on by itself, no aco restart needed.
- Follow the log: `tail -f data/daemon.log`.

### Start everything (background)

```sh
FV=~/Documents/ChatGPT/jev/FreeVideo-local
(cd $FV/ComfyUI && nohup ../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188 >/dev/null 2>&1 &)
(cd ~/workplace/video-agent && nohup uv run va daemon --port 8790 >> data/daemon.log 2>&1 &)
```

### What is running

```sh
pgrep -fl "ComfyUI/main.py"; pgrep -fl "va daemon"
curl -s -o /dev/null -w "ComfyUI %{http_code}\n" http://127.0.0.1:8188/system_stats
curl -s -o /dev/null -w "aco %{http_code}\n" http://127.0.0.1:8790/api/system
```

Both `200` means all good.

### Stop
- aco: `Ctrl-C` in the foreground; `pkill -f "va daemon"` in the background.
- A job already running inside ComfyUI is **not cancelled by stopping aco**; the next aco start reconciles it by prompt_id and collects its outputs.
- Before restarting aco, check the console shows no running task and the agent isn't mid-answer.

### Console only
`uv run va serve` opens the console (browse + review) without rendering; queued tasks run once the daemon is up.

## 3. Daily use

### 3.1 A full creative loop
1. **Ask** — in the console (*Projects & Chat*) or in Telegram, in English or Chinese. Example: “Same blonde academy girl, half-body, looking back at the camera, warm evening window light.” You can **attach a reference image** (📎 in the console, or send a photo in Telegram) and say “use this face”.
   A photo sent in Telegram **without a caption** doesn't start anything: aco confirms it and your next message within 30 min carries it along (e.g. send the casting photo, then “make a video with this face”). When the agent asks for a picture, just send it — that is the answer.
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
| `/round TEMPLATE [COUNT] [prompt]` | Queue a round without the LLM (see `va templates`, e.g. `zimage-t2i` `flux2-t2i` `flux2-edit` `h3-t2v` `h3-i2v`) |
| `/redo` | Redo the latest round under review |
| `/reopen [ROUND_ID]` | Change the decision of the latest (or given) decided round |
| `/pause` `/resume` | Pause / resume rendering (the running task finishes) |
| `/cancel` | Cancel this project's queued tasks and interrupt our running task |
| `/mode [on_demand\|session\|always\|command_only]` | LLM residency (default on demand) |
| `/project` · `/projects` · `/project new NAME` · `/project use NAME` · `/project done` | Projects |
| `/session` · `/session new [TITLE]` · `/session use N` | Sessions (N: 1 = most recent) |
| `/memory` · `/memory add TEXT` · `/memory del ID` | Project memory |
| `/pack` | Current baseline pack |
| `/model [NAME]` | Show / switch the agent's brain (local or online, see §3.10) |
| `/llm` · `/llm use NAME` · `/llm load` · `/llm unload` | List the downloaded local models; switch aco's local model (unloads the old one, checks memory, tells you when loaded). Same as `va llm …` on the command line |
| `/permissions [default\|bypass\|forget RULE\|all]` | Approval mode and always-allow rules (see §3.10) |
| `/fv TASK` | Hand a video task to the FreeVideo agent |
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
To switch the brain on purpose use `/model` (see §3.10); this section is the **automatic fallback** when the local LLM can't run.

When the local LLM can't run — most often because a video render holds the memory — requests normally wait. You can let them continue online instead, in `config.toml`:

```toml
allow_online = true                                   # privacy switch, off by default
llm_fallback = ["anthropic", "claude_code", "codex"]  # tried in order
```

- `anthropic`, `openrouter` (or any `openai_compatible` endpoint): the online API continues the same request step by step. Keys: `ANTHROPIC_API_KEY` / `ant auth login` or `anthropic_api_key`; `OPENROUTER_API_KEY` or `openrouter_api_key` (plus `openrouter_model`).
- `claude_code` / `codex`: the local CLI, with its own login and online model, runs the whole request. It gets **only aco_lw's tools** over MCP (Claude Code with all built-in tools disabled, Codex in a read-only sandbox); every tool call still goes through the execution policy, approvals and the scheduler.
- Without `allow_online = true` nothing is sent out, and the console shows the request waiting for resources with the reason.
- Chat bubbles show which backend answered ("via …") and, for Claude Code, the cost.

### 3.8 Productions: from a short story to a film (phase 3, preview)
A production runs a **workflow** stage by stage; every stage waits for your approval before the next one starts. The first workflow is `novel-to-film`:

| Stage | Who does it | You review |
|---|---|---|
| Adaptation (logline, scenes, beats) | LLM with skill `adapt-novel` | approve / redo with a note / edit the JSON |
| Story bible (style, characters, locations) | LLM with skill `story-bible` | same |
| Character sheets, location plates | FLUX.2 rounds, one per character / location | select a candidate in **Review** |
| Storyboard (framing, camera, motion, reference per shot) | LLM with skill `storyboard` | approve / redo / edit |
| Keyframes (edited from the approved sheets) | FLUX.2 edit, one round per shot | select in Review |
| Shot videos | H3 image-to-video per shot — **starts only when you press Start** | select / redo |
| Edit (order, trims, transitions, subtitles) | LLM with skill `edit-timeline` | approve / redo / edit |
| Final cut | FFmpeg renders the timeline (keeps the clips' own audio — H3 voices dialogue; soft subtitles + `.srt`) | approve |

- **Inputs**: a story *or* a description of the film you want, plus optional **links** (web pages: readable text + main image; YouTube: title, description, chapters, transcript, thumbnail and `youtube_frames` frames) and **reference images**. For each image give a name, a type (character / location / style / prop) and how to use it: *Redraw in the film's style* (FLUX edits a character sheet from your photo) or *Use as is* (your photo becomes the approved sheet). Everything is gathered into a **brief** first; a link that fails is marked and the rest continue.
- **Use my image…** on any render item (character sheet, location plate, keyframe, even a shot video) replaces it with your file as the approved version; what depends on it is re-rendered.
- **Console → Productions → New**: paste the text, choose the video tier (default `smoke`, measured) and the number of image candidates. Planning stages start right away (local LLM; they wait while a video renders unless `allow_online` + `llm_fallback` are set).
- **Changing something upstream** only invalidates what depends on it: editing one character re-renders that character's sheet and the shots that use it; other shots stay approved. A planning stage whose input changed shows *upstream changed — keep or redo?*.
- **Dialogue language**: set it when creating the production (or later under *Settings*). Storyboard and edit write frames, motion and dialogue in it, and every video prompt asks for speech in it — H3 voices dialogue in the prompt's language.
- **A change that concerns many shots** ("every shot in Mandarin", "darker grade throughout"): open the storyboard stage and *Redo…* with that note. The approved storyboard stays valid until you approve the new one; afterwards only shots whose text changed are re-rendered (keyframes automatically, videos after you press Start). A reject note on a single video round only changes that round.
- Telegram gets a message when a planning stage is ready for review; render rounds arrive with the usual buttons.
- The chat agent (and Claude Code / Codex over MCP) can drive productions with `create_production`, `get_production_plan`, `run_stage`, `review_stage`; video stages still need your go-ahead.
- Extending: a workflow is a YAML file in `workflows/` (or `data/workflows/`), output contracts are JSON Schemas in `workflows/schemas/`, and skills are Claude-Code-style `SKILL.md` folders in `skills/` (or `data/skills/` for your own). A new kind of film = a new YAML + skills, no code changes.

### 3.9 Consistent characters with FreeVideo films; ComfyUI tools for the agent
- **Workflow `story-to-film`**: characters become **FreeVideo cast cards** (up to six labelled pictures + a voice; an existing card with the same name is reused, otherwise one is made from the approved sheet). Shots are filmed as **one FreeVideo film**: each shot gets `video_candidates` takes with the cards as references, and a shot marked *continue* is shot on from the last frame of the take you chose. Takes arrive in **Review** as a round; selecting one tells FreeVideo; *redo / reject with a note* becomes a FreeVideo reshoot with your note. Continued shots need ≥ 10 s and quality light/medium. Pause → *stop now* cancels unfinished takes.
- **Workflow `novel-to-series` (novel → series episode, faithful)**: filmed like `story-to-film`, but you give it one chapter (or a few) per production and get an episode of about `episode_minutes` minutes (default 5). The adaptation keeps every scene, action and line of the source (`fidelity: faithful`); inner monologue is shown, not narrated — looks, gestures, inserts, pauses (`interiority: visual`). The storyboard is written **scene by scene** and joined and renumbered, so 35–50 shots fit the local LLM's reply size; each scene gets a share of the time by its number of beats. One take per shot by default: at the measured ~3 min of rendering per second of video (480×864), a 5-minute episode is about 15 h (estimate), so check the storyboard before you start shooting. Long novels go in episode by episode.
- **Direction (`direction` stage, in `story-to-film` and `novel-to-series`)**: before the storyboard, one camera design for the whole episode — visual thesis, preferred / avoided moves, shot sizes, lenses, composition, light and colour, average shot length, axis rules, motifs — plus a camera strategy and key moment per scene. Style: **say it** — `style` when creating the production ("Wong Kar-wai", "Hou Hsiao-hsien long takes", or a description), or tell the agent in chat and it updates the setting and reruns this step; **or let it propose** 2–3 options that fit the story, with the full plan for its pick — approve, or *redo* with "option 2". The scene-by-scene storyboard must follow the plan. The style reference is the 20 director lenses of DirectorSKILL (MIT), condensed in `skills/film-direction/references/`.
- **Workflow `novel-series` (whole novel → series)**: give the whole book.
  1. **Chapters**: split in code by headings ("第X章 / 第X回 / 卷X / Chapter N"), or by length when there are none; chapters longer than `chapter_chars` (default 6000) are split into parts.
  2. **Chapter notes**: the local LLM reads them one chapter at a time — never the whole book in one call.
  3. **You approve the episode plan once**: chapters are grouped into episodes of about `episode_minutes`.
  4. **Decided once for the whole series**: the bible, casting, cast cards and camera direction (you review casting, the bible and the direction once each).
  5. **Episodes are created for you**: one `novel-episode` production per episode, reusing the series' cast, cards and style; each adaptation gets the previous episodes' synopses.
  6. **One after another**: only episode 1 starts; each next one starts when the previous has finished shooting.
  - `episode_review` sets how much you review per episode: `final` (default) only the final cut, `storyboard` the storyboard too, `all` every stage.
  - `auto_shoot: true` starts each episode's shooting without waiting for you.
  - No shot exceeds `max_clip_seconds` (default 10; one generation renders only that long). Longer continuous moments become consecutive shots that continue each other and are joined in the edit; an over-long storyboard gets one rewrite.
- Prompts follow MiniMax's official **h3-prompt-writing** skill (installed in `data/skills/`); FreeVideo compiles each shot into the H3 reference format.
- **ComfyUI tools (artokun/comfyui-mcp, run by our daemon)**: the agent can list models and nodes, read and build workflows, read model-family guides (`comfy_catalog`, `comfy`). Running a workflow goes through our queue (`run_comfy_workflow`, tracked, pause and memory gates apply). Installing nodes, downloading models or training needs your approval (buttons); restarting ComfyUI, deleting models, touching other jobs in the queue, freeing VRAM, changing ComfyUI settings and cloud/paid nodes are refused.
- **FreeVideo approvals**: FreeVideo can require you to approve film starts and long reshoots in its own page (ComfyUI → Extensions → FreeVideo → agent chat). The setting is the file `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI/output/FreeVideo/.agent/confirm.json` (no UI toggle in this FreeVideo version; defaults `{"start": true, "reshoot_seconds": 300}`). **Set on 2026-10-27 at the user's request to `{"start": false, "reshoot_seconds": null}`** — films start and reshoots run without asking in FreeVideo; our own Start button and review gates still apply. To re-enable, set `start` to `true` and/or `reshoot_seconds` to a number of seconds. FreeVideo re-reads the file on every check.

### 3.10 General agent: tools, vision, online brains, permissions (2026-11-07)
Free text goes to the agent. The LLM only **proposes** tool calls; the orchestrator runs them and hands results back when the LLM can take them (held while the LLM is unloaded). It picks its own tools and skills:

| Tool | What it does | Approval |
|---|---|---|
| Generation tools (`queue_round`, `wait_for_round`, productions, ComfyUI exploration…) | Images, video, review, productions | Unmeasured video tiers, only when the estimate is over 20 min (`budget_min` in `policies.yaml`) or unknown; never when your message named the tier |
| `web_fetch` | Read a page as text (no login, no JS) | No |
| `run_command` | zsh command in the run's working dir `data/agent-work/<run>/` (e.g. agent-reach, ffmpeg) | **Yes** (see permissions) |
| `read_file` / `write_file` | Read `data/` and skill dirs; write the working dir only | No |
| `list_skills` / `load_skill` | `data/skills/` and `~/.claude/skills` (`agent_skill_dirs`) | No |
| `memory_read` / `memory_write` | The agent's own memory `data/agent_memory/`, indexed in its system prompt | No |
| `look` | **Vision**: images shrunk to 1024 px for the vision LLM; videos as 6 frames; `@round:ID` for a whole round | No |
| `add_media` / `send_file` | Turn a file into `@upload:ID` / send it to you (paths may be `@task:ID`) | No |
| `mcp_list` / `mcp_call` | `[mcp_servers.*]` in `config.toml` (Claude Code format; Exa for now) | Read-only tools no, others yes |
| `delegate_agent` | Hand a sub-task to Claude Code / Codex and wait for the report | Yes; needs `allow_online` |
| `delegate_freevideo` (`/fv`) | Hand a whole video task to the FreeVideo agent | Yes; needs `freevideo_agent_online` |

**Hard refusals (in every mode)**: direct ComfyUI `:8188`, reading the FreeVideo token, `kill` / `pkill`. Loading / switching the LLM (`va llm use`, `lms load`) is an ordinary approved command. Commands can carry `memory_gib` / `exclusive`; after approval the orchestrator starts them only when that much memory is free (waiting for a video, unloading the LLM for exclusive ones), and the approval message shows the estimate. The execution policy is `policies.yaml` (per-tool switches, limits, MCP approval rules).

**Permission modes** (like Claude Code's permission modes):
- `default`: actions that need approval come with buttons **✅ Approve / ✅ Always (rule) / ✕ No**. *Always* stores a rule so the same kind is not asked again: commands by program (`cmd:ffmpeg`, single commands only — no pipes, chaining or redirection), MCP by tool (`mcp:exa.web_search_exa`), hand-overs by target (`delegate:codex`), unmeasured video tiers by template (`round:h3-t2v`).
- `bypass`: **no approvals** — actions run directly (like `bypassPermissions`). Each auto-run still posts a one-line “⚡ Auto-approved” note; hard refusals, the execution policy and the online switches still apply; **installing ComfyUI nodes / downloading models still asks every time** (hard rule). `/status` shows ⚡.
- Switch with `/permissions bypass` · `/permissions default` · `/permissions` (mode and rules) · `/permissions forget <rule|all>`. Start-up default: `permission_mode` in `config.toml` (`/permissions` is stored and wins).
- Static pre-approval also exists: `run_command.auto_allow` (command prefixes) in `policies.yaml`.

**Online brains** (need `allow_online = true`; when it is off the agent falls back to local and sends nothing out). `/model` shows, `/model <name>` switches:

| Name | What | Needs |
|---|---|---|
| `local` | Local 27B (LM Studio, a vision model) | — |
| `openai` | OpenAI API (ChatGPT's models) | `openai_model` + `OPENAI_API_KEY` / `openai_api_key` |
| `openrouter` | Any model on OpenRouter | `openrouter_model` + `OPENROUTER_API_KEY` / `openrouter_api_key` |
| `anthropic` | Claude API | `ANTHROPIC_API_KEY` / `ant auth login` |
| `openai_compatible` | Any OpenAI-compatible endpoint | `openai_base_url` + `openai_model` |
| `codex` | Whole request to the Codex CLI | Local Codex login (a **ChatGPT account** works; no API key) |
| `claude_code` | Whole request to Claude Code | Local Claude Code login |

A ChatGPT subscription is not an API: use it through `codex`; `openai` bills an API key. Codex / Claude Code only get aco_lw's tools (over MCP), and commands still follow the permission mode.

### 3.11 Character LoRAs: keep one person
- **Where**: put LoRA files in `FreeVideo-local/ComfyUI/models/loras/`. aco reads each file's base model, lists it in the agent prompt and says which templates take it.
- **Notes (recommended)**: in `data/loras.yaml` (not in git), give each LoRA a person, style, strength and trigger word, e.g.:
  ```yaml
  my-face.safetensors:
    person: heroine
    style: photo
    strength: 0.5
    trigger: null
  ```
  No restart needed: the next conversation picks it up.
- **Templates**: `flux2-lora-t2i` (text-to-image) and `flux2-lora-edit` (reference image + LoRA). `lora` is required; `lora_strength` is usually 0.45–0.6.
- **Locking the person in video**:
  - These are Klein 4B image LoRAs and cannot be loaded into the H3 video model. Make images of the person with the LoRA first, then hand those to the video.
  - The agent loads the `character-consistency` skill: LoRA front portrait and per-shot first frames, then `freevideo-ref` with no reference video, just two reference pictures, so the face holds through the whole clip.
  - Long videos are split into 5–10 s shots, each with its own first frame, then joined.
  - `h3n-r2v` doesn't run on a Mac (int8 matmul missing on MPS).
- **H3 video LoRAs** (camera moves, dance / motion, style, adult): template `h3-lora-ref`. The character picture is a reference for the whole clip; `lora` / `lora2` / `lora3` stack up to three LoRAs, and their trigger words go into the prompt. Many community LoRAs record no base model, so add `base: minimax-h3` in `data/loras.yaml` for the agent to match them to the template. Adult LoRAs are only for clearly adult fictional characters.
- **Confirmed by you**: the agent proposes "character → LoRA, strength" with `propose_memory`; once you confirm, the project uses it from then on.

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
| “ComfyUI offline” | Open http://127.0.0.1:8188 | Start it as in §2 “Start ComfyUI + FreeVideo”; aco won't start it, and carries on once it is up |
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
- Only the orchestrator talks to ComfyUI; the LLM only proposes tool calls and the orchestrator runs them. Commands are approved one by one by default (or by an always-allow rule / the bypass mode you turned on); hard refusals apply in every mode.
- Never cancel or free other people's jobs; `/free` double-checks that the queue is empty.
- Don't change ComfyUI launch options or global LM Studio / OpenClaw settings; downloading models or installing custom nodes needs your approval.
- Online: nothing from the conversation is sent out unless `allow_online` is set (`/fv` only checks `freevideo_agent_online`).
- Telegram answers whitelisted chats only; the console binds to 127.0.0.1.
- Project memory written by the LLM needs your confirmation.

## 8. Known issues (2026-10-26)
1. The planner occasionally misreads ambiguous words (“too small” meaning age vs framing). Specific notes work best; *View LLM context* shows what it saw.
2. Planner replies take 10–20 s, mostly model “thinking” that the API can't switch off for this model (`/no_think` halves it; see `planner_fast`).
