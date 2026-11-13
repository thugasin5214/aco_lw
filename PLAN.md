# Lightweight local video agent · Implementation plan

[中文](PLAN.zh-CN.md)

Design basis: `docs/design.html` (design doc · revised after review 2026-10-26). This file turns the design doc into an executable scope, stages and task lists; design trade-offs follow the design doc, this file only says "what to build, in what order, and what counts as done".

## 1. Goal in one sentence

A **deterministic Python orchestrator** owns every side effect (submit / poll / release / retry) and drives the local ComfyUI to make images and videos; the LLM (LM Studio 27B) is only an optional planner, woken on demand; Telegram handles notifications and picking; OpenClaw joins last as a sub-agent.

## 2. Scope

### This round (in scope: stage 0 + stage 1, delivered up to G1)

| Module | Content |
|---|---|
| Baseline measurements (stage 0) | LM Studio model load / unload time, ComfyUI `/free` release check, image / video model switch time; **one 640×384, 3-second video smoke test**; record peak memory. Outputs `data/baseline.json` + `docs/baseline.md` |
| ComfyUI client | Thin wrapper over `/prompt`, `/queue`, `/history`, `/free`, `/object_info`, `/system_stats`, `/upload/image` |
| Template registry | Imported from the existing API JSONs (01 text-to-image, 03 image-to-video, 04 text-to-video, Z-Image); each template has a slot map (parameter name → node ID.field) with range constraints |
| Parameter tiers | Images `1280×720`; video starting at `640×384×3s`, raised step by step; production tasks can only pick a tier |
| Schema validation | pydantic checks parameter ranges and unknown fields; node types validated against `/object_info` |
| Memory gate | Before submitting, check available memory + the measured peak in the model registry; wait or refuse when short |
| Task state machine | draft → validated → waiting for memory → submitted → running → review → done / rejected / failed / timeout |
| Persistence | One directory per task + `manifest.json` (parameters, seed, template version, duration, peak memory, prompt_id); SQLite as the index |
| Recovery | After a process restart, reconcile by `prompt_id` against `/history`; **never resubmit** |
| Release policy | `/free` only when the task is over and the queue is idle, confirming the queue twice before releasing; `--keep-loaded` keeps the same model |
| Resource lock | Single-machine serial lock (file lock), same semantics as the existing `media_bridge.py` |
| CLI | `va plan` (placeholder, the LLM arrives in stage 2), `va submit`, `va status`, `va release`, plus a `va run <taskfile>` batch entry |
| **Web progress console (read-only, added 2026-10-26)** | `va serve`: system status (available memory, pressure, queue, whether the LLM is loaded); task DAG (by parent / child, e.g. casting image → image-to-video); per-task stage pipeline (validate → gate → submit → generate → collect → review) with durations; output gallery (images / videos previewed directly); project progress page `docs/progress.html` |

### Explicitly out of scope (this round)

- Telegram, review **actions** (picking / rejecting on the web), director's baseline pack UI (stage 2; the web is read-only this round)
- LLM planning / free-text understanding / residency mode switching (stage 2)
- Consistency scoring (faces, style embeddings), exploration budget, L3–L5 exploration (stage 3)
- OpenClaw sub-agent, search, skills, MCP (stage 4)
- Model selection and quantisation, node-level tuning, fixing OpenClaw's SQLite warning (excluded in design doc §1)
- Changing ComfyUI startup parameters, the global OpenClaw / LM Studio config, downloading models, installing custom nodes

### Hard rules (across all stages)

1. The LLM and Telegram **never touch** the ComfyUI API or a shell directly: the agent only proposes tool calls, the orchestrator executes them. (Revised 2026-11-07: the orchestrator gained generic tools — reading web pages, running commands (each approved by the user), reading / writing the work directory, skills, agent memory; generation side effects still go only through the generation tools. See `src/video_agent/toolbox.py`.)
2. Never cancel or release tasks and models not submitted / loaded by this system; refuse `/free` while the queue is not empty.
3. Every number is labelled **measured / estimated**; estimates go on the verification list.
4. Outputs, database and logs all live under this project's `data/`; deleting the directory uninstalls it.

## 3. Existing assets (reused, not modified)

> Verified 2026-10-26: what runs on :8188 is **FreeVideo-local**'s ComfyUI, not `~/workplace/localvideo`.

| Asset | Location | Use |
|---|---|---|
| ComfyUI 0.34 (:8188, find the PID by port) | `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` | Executor; custom nodes FreeVideo, ComfyUI-GGUF, PuLID |
| Video engine | FreeVideo (VDN-driven **MiniMax H3**, node `FreeVideoGenerate`), weights `FreeVideo-local/models/{vdn,encoder}` ≈ 28 GB | Video smoke-test subject. Decides how many layers stay resident from the **currently available unified memory** and streams the rest from disk → under memory pressure it gets **slower** rather than OOM |
| Image models | FLUX.2 Klein 4B Q8 GGUF (01/02); `z_image_bf16` (07 Z-Image) | Image loop |
| API templates | `~/Documents/ChatGPT/jev/workflows/标准实验室/0{1,2,3,4}-*-API.json` | Source for the template library |
| Old bridges | `.../openclaw-local-media/media_bridge.py`, `标准实验室/api-tools/comfy_job.py` | Behaviour reference (serial lock, no duplicate submits, release when idle) |
| LM Studio (:1234, `lms`) | `qwen3.8-27b-splash` (17.38 GB, resident) | Stage 0 measures load time; stage 2 planner |
| OpenClaw | `~/.local/bin/openclaw` | Stage 4 |
| Old environment (not used) | `~/workplace/localvideo` (Sulphur 2 / LTX-2.3) | Reference only |

**Model-agnostic**: video models will change (newer MiniMax H3, LTX 2.5, ...), so the template registry + model registry must be pluggable: a new model = a new template directory + slots.yaml + one G0-style measurement record; orchestrator code does not change.

## 4. Stages and gates

| Stage | Content | Gate (exit condition) | This round |
|---|---|---|---|
| **0 Interfaces and baseline measurements** | See §5 | **G0**: video smoke test passes; peak memory and switch times recorded | ✅ **Passed 2026-10-26**, see `docs/baseline.md` |
| **1 Scheduler + image loop + web progress console** | See §6 | **G1**: CLI runs the image loop end to end; no duplicate submit after interrupting and restarting; the web shows the task DAG and outputs | ✅ **Passed 2026-10-26** (21 s per image; after kill -9, recover only reconciled, ComfyUI saw a single submit) |
| 2 Telegram + review + project history | Notifications and button picking, console review, director's baseline pack, LLM planning (schema-validated), projects and chat history | G2: picking can be done on the phone; no LLM inference while waiting | **In progress**: code done, waiting for the Telegram token for the G2 hands-on check |
| **3 Production pipeline (revised 2026-10-27)** | Novel → adaptation → bible and casting → storyboard → keyframes → shot videos → edit → finished film, each step approved before the next; the focus is **infrastructure and extensibility**: declarative workflows, an artifact store with versions and staleness propagation, an authoritative plan, a skill executor, an FFmpeg executor, batch scheduling by resource family (see §6c-6) | G3: a ≤ 1-minute short story (6–10 shots, smoke tier) runs from text to finished film; changing one character only makes the related downstream artifacts stale; restarts lose no progress; swapping the workflow YAML needs no code change | **Planning** (waiting for the user to confirm the open items in §6c-6) |
| 4 Extensions | ~~OpenClaw sub-agent~~ → the orchestrator's own generic tools + MCP (§6c-7) | G4: started on demand, can exit, no orphan processes | Done (2026-11-07) |
| **2.8 Agent runtime + Executor + resource scheduling** | LLM tool calls executed by the orchestrator, results handed back when possible; Executor interface; dynamic resource scheduling (see §6c-3b) | G2.8: a one-line request runs "check status → image → wait → decide → video"; continuations are parked during video and resume automatically afterwards; restarts recover | **Next** |
| **5 Installable package + setup wizard** | One-step install, web setup wizard + settings page, install docs, launchd service, upgrade / uninstall (see §6d) | G5: on a clean Mac, the first image within 30 min following only the install docs (model downloads excluded); recovers automatically after reboot | **Postponed** (user decision 2026-10-26: do W2 first) |
| 6 Online models and external agents | OpenAI / Anthropic backends, aco_lw MCP server, Codex / Claude Code integration, budget and privacy switches (see §6c-4) | G6: switching local / online loses no session or memory; falls back when offline; agents cannot go beyond the MCP whitelist | Planning |

G0 conclusions (details in `docs/baseline.md`): the LLM loads in only about 15 s; video peaks at about 54 GiB and coexisting with the LLM reaches the memory-pressure warning level → **by default unload the LLM before submitting video, but not for images; unload the idle LLM after 5 min with `--ttl`; the gate looks at system available memory + pressure level, not process RSS**. The two defaults G0 was meant to rewrite in the original plan: **residency policy** (switching is slow → keep the generation model resident during a review session) and **tier ceiling** (peak memory is tight → strictly stagger, cap the video tier).

## 5. Stage 0 task list

- [x] **T0.1 Memory sampler** `bench/memsample.py`: every 0.5 s in the background, sample `vm_stat` / `memory_pressure` and the RSS of the ComfyUI and LM Studio processes (psutil), write CSV; report the peak and swap change.
- [x] **T0.2 LLM load / unload time** `bench/llm_load.py`: `lms load qwen3.8-27b-splash` → first token → `lms unload`, 3 times each; record footprint after loading and the quantisation; check whether `--ttl` idle unloading works per instance.
- [x] **T0.3 ComfyUI release check** `bench/comfy_free.py`: `/free` when the queue is idle, compare RSS and system available memory before and after (re-checking the trial report's 9.3 → 0.82 GiB).
- [x] **T0.4 Image smoke test + switch time**: 01 (FLUX.2 Klein), 1280×720, once cold and once warm.
- [x] **T0.5 Video smoke test**: 04 text-to-video (FreeVideo / MiniMax H3, no casting image needed) 640×384, 3 s, two_pass 8+3 steps, **once**; record peak memory, total time and per-stage times. ✅ Approved by the user (VM running).
- [x] **T0.6 Coexistence test**: run T0.5 once with the 27B loaded and once unloaded, compare time and swap (FreeVideo adapts, so the question is how much slower it gets).
- [x] **T0.7 Summary** `docs/baseline.md`: tables + conclusions (residency policy, tier ceiling, whether strict staggering is required).

## 6. Stage 1 task list

- [x] **T1.1 Project skeleton**: uv + Python 3.11; dependencies `httpx`, `pydantic`, `typer`, `psutil`; `ruff` + `pytest`.
- [x] **T1.2 ComfyUI client** `comfy.py`: synchronous httpx, timeouts and retries; unit tests with recorded responses.
- [x] **T1.3 Template registry** `templates/<name>/{api.json, slots.yaml}` + `registry.py`: slot definitions (node ID, field, type, range, default); import 01/03/04/Z-Image.
- [x] **T1.4 Tiers and schema** `presets.py`, `schema.py`: pydantic models; refuse out-of-range and unknown fields; `/object_info` node validation (cached).
- [x] **T1.5 Rendering** `render.py`: template + parameters → a submittable prompt JSON (pure function, unit-testable).
- [x] **T1.6 Storage** `store.py`: SQLite (tasks table: id, state, template, params_hash, prompt_id, timestamps) + `manifest.json` in the task directory; state transition functions live in one place and validate legal transitions.
- [x] **T1.7 Memory gate** `memgate.py`: system available memory + `kern.memorystatus_vm_pressure_level` vs the model registry peak (G0: video 54 GiB) + 8 GiB margin; when short, in order: `/free` (idle only) → unload the LLM → queue and wait.
- [x] **T1.8 Scheduler** `scheduler.py`: file lock → dedup (params_hash + unfinished tasks) → gate → `/prompt` → poll `/history` (with timeout) → download outputs → write manifest → release policy.
- [x] **T1.9 Recovery** `recover.py`: on start, scan `submitted / running` tasks and reconcile by prompt_id; missing from both `/history` and `/queue` → mark failed, **never resubmit automatically**.
- [x] **T1.10 CLI** `cli.py`: `va submit/status/release/run`; `plan` returns "not enabled" for now.
- [x] **T1.12 Web progress console** `web/`: FastAPI read-only API + single page (plain JS + SVG DAG), polling every 3 s; outputs previewed via local routes.
- [x] **T1.13 Progress document** `docs/progress.html`: stage / task / gate status, can be opened directly in the web console.
- [x] **T1.11 G1 acceptance**: run one 1280×720 image; kill the process while "running" → restart → only reconcile, never resubmit; outputs and manifest complete.

## 6b. Stage 2 task list: Telegram + review flow (G2)

**Goal**: on the phone, a person completes "make a set of candidates → pick / refine / reject → lock into the baseline pack"; no LLM inference while waiting for human review.

**New concepts**
- **Round of candidates**: one request generates N candidates (same parameters, different seeds; images default to the preview tier) as one unit of review. The tasks table gets `round_id`; the DAG groups by round.
- **Long-running service `va daemon`**: one process runs three things — the Worker (consumes `draft` tasks serially, i.e. the existing orchestrator), the Telegram long-polling listener (loads no model) and the web (progress + review console). The CLI still works and shares the serial lock with the daemon.
- **Project**: the outermost grouping, one project per creative goal (e.g. "blonde academy girl · looking-back shot"); rounds, tasks, baseline packs and chats hang under a project. A project can be marked done and is **kept forever**; the console can browse all past projects (including done ones).
- **Chat history**: every inbound / outbound message (Telegram, web review actions, LLM planner replies, system notices) is stored in the `messages` table, viewable per project.
- **Director's baseline pack**: versioned locked assets (character reference images, style references, video motion parameters); every lock creates a new version; later tasks can reference `@pack:role`.

**Tasks**
- [x] **T2.1 Rounds**: `rounds` table + `va round <template> -n 4`; once the N tasks of a round have run serially, the whole round enters "review".
- [x] **T2.2 Worker / daemon**: `draft` means queued; the daemon loop takes tasks and runs them; `/pause` `/resume`; `recover` first on start.
- [x] **T2.3 Review actions (orchestrator layer, deterministic)**: pick → lock (seed, parameters, output) into the baseline pack; refine → fixed seed + parameter delta in a new round; reject + note → handed to T2.6 to translate into a parameter delta for a new round. "Mix A+B" needs a dual-reference template, postponed to stage 3.
- [x] **T2.4 Baseline pack**: `packs` table + `data/packs/<name>/v<N>/`; `va pack show/lock/history`; task parameters can reference images in the baseline pack.
- [x] **T2.5 Telegram listener**: whitelisted chat_id; commands `/status /tasks /pause /resume /cancel /mode /pack` run directly without waking the LLM; a round of candidates is sent as **files** (uncompressed) + inline buttons (pick 1…N / refine / reject); completion notices.
- [x] **T2.6 LLM planner (woken on demand)**: free text → immediate "got it" → `lms load --ttl 300` (G0: ~15 s) → JSON parameter delta → **validated against the template slot schema**, asks back if invalid; context rebuilt from the database (baseline pack + last few rounds), chat is not replayed; free text queues while generation holds memory.
- [x] **T2.7 Review console (web, actionable)**: a round's candidates side by side in a grid, compared with the baseline pack, keyboard shortcuts (1–9 pick, F refine, R reject), usable on the phone; every button calls the same orchestrator functions as Telegram.
- [x] **T2.8 Residency mode `/mode`**: wake on demand (default) / session resident / commands only; the memory gate takes precedence over the mode.
- [x] **T2.10 Projects and history**: `projects` table (active / done / archived); `/project new|use|done`; a new "Projects" view in the console: project list (including done) → project detail (round timeline, DAG, outputs, baseline pack versions, chat history).
- [ ] **T2.9 G2 acceptance** (2026-10-26: the console side has gone through "request → 4 candidates → reject + note ×2 → new round"; Telegram whitelist and push are configured, **picking on the phone is waiting for the user to test**): receive a round of 4 candidates on the phone → pick 1 and lock it into the baseline pack → send a note → the new round changes parameters per the note; logs prove the LLM did no inference while waiting for review (unloaded by TTL).

**Not in this stage**: consistency scoring, exploration budget, mix A+B, OpenClaw.

## 6c. To-dos (from the user's tests on 2026-10-26)

- [x] **B1 Reference images in chat**: the console chat supports uploading / pasting a local path, Telegram supports sending images directly; the planner picks reference editing (`flux2-edit`) or image-to-video accordingly, and can lock it into the baseline pack in one step.
- [x] **B2 Free text belongs to the latest round in review by default**: when a round awaits review, typing without pressing a button = "reject + note" on the latest round (the DAG links to the previous round, which closes automatically); start with "新需求：" (new request) to start a new line.
- [x] **B3 Planner context**: include the previous round's chain of notes and the user's own words, to reduce ambiguity like "too small"; the reply restates the points it understood so the user can correct them.
- [x] **B4 Planning time**: root cause was prefix-cache misses (solved by S8: multi-turn reuse 55–73%, first token 2–3 s) + 600–1500 tokens of thinking output. Measured turning thinking off: `reasoning_effort` / `enable_thinking` have no effect on this model; `/no_think` 14.4 s → 9.3 s, made into the optional `planner_fast` (off by default to keep understanding quality).

## 6c-2. Stage 2.5: sessions and project memory (proposed by the user, 2026-10-26) — done and merged into main (2026-10-26)

**Before**: no notion of a session. Every LLM call was stateless; context was assembled ad hoc from the database: project name + baseline pack roles + the prompts of the last 3 rounds (300 characters each) + this message. **The chat history itself never reached the LLM**, so references like "what I just said" or "the previous version" were often lost; preferences accumulated in a project (e.g. "35mm, cinematic, refined CG leaning photoreal") had nowhere to settle.

**Target structure**: project → several sessions; project memory is shared across sessions.

| Layer | Content | Lifetime | Written by |
|---|---|---|---|
| **Project memory** | Character settings, style conclusions, user preferences, taboos, key decisions (each with its source: message / round ID) | Permanent across sessions; versioned, can roll back | The LLM **proposes**, written after the user confirms (prevents memory pollution, see design review #8); the user can also edit / pin by hand |
| **Session summary** | Rolling summary of this session so far | With the session | Compacted automatically near the context limit (the LLM summarises older messages) |
| **Recent session messages** | The last K messages verbatim (including LLM replies and review actions) | With the session | Automatic |
| **Live status** | Baseline pack, rounds in review, prompts and decisions of the last few rounds | Read live from the database | System |

**Context of every LLM call** (in cache-friendly order, see S8) = system rules + template docs + project memory + session summary + session messages + live status + this message; assembled within a token budget (target ≤ 16k; the 27B has 131k context, leaving room for thinking and keeping time down).

**Tasks**
- [x] **S1 Data**: `sessions` table (project, title, created / last active, summary, state); `messages` gets `session_id`; `memories` table (project, content, category, source, state: proposed / active / archived, version). Old messages go into each project's "history session".
- [x] **S2 Session operations**: the console's project page lists that project's sessions on the left: new / continue / rename; Telegram `/session new [title]`, `/session list`, `/session use N`; by default continue the project's most recently active session.
- [x] **S3 Context assembler**: assemble per the table above, truncate within the budget, and record which memory entries each call used (traceable).
- [x] **S4 Automatic summaries**: when a session's estimated tokens exceed a threshold (e.g. 12k), compact older messages into the session summary; the original messages stay in history, they just no longer reach the LLM.
- [x] **S5 Project memory proposals**: after each "approve / reject + note", the LLM proposes 0–3 memories (e.g. "apparent age: about 19, refined CG leaning photoreal; keep the lens at 35mm"); the console and Telegram show "remember / ignore" buttons; active memories reach every later session.
- [x] **S6 Memory management page**: view, edit, pin, archive and roll back memories in the project page.
- [x] **S8 Prompt caching (requested by the user, measured to work 2026-10-26)**: LM Studio reuses the prefix KV cache for this 27B — a 17.4k-token context takes ~48 s cold, same prefix with a different ending **0.3–0.5 s**; changing one character at the start recomputes everything; the engine keeps several caches at once (switching back to an old prefix still hits). Design requirements:
  - **Stable prefix order**: system rules → template docs (static) → project memory (changes rarely) → session summary (changes only on compaction) → session messages (append-only) → **last** the live status (rounds in review, memory and other volatile content) and this message. Timestamps, counters and other volatile content must not appear in the prefix.
  - **Append-only**: session messages are appended verbatim, history is never rewritten; compaction is the only point where the prefix is rewritten, so the threshold is high and it is rare, with a prewarm right after.
  - **Prewarm**: after the LLM was unloaded for video and reloaded, send one `max_tokens=1` request with the current session prefix in the background, so the user's next message does not wait 48 s.
  - **Longer TTL while a session is active**: the cache only lives while the model is loaded; during an active session the TTL extends to 20 min (configurable), then unloads as usual when idle.
  - **Observable**: every call records prompt_tokens, time to first token and whether it looks like a cache hit (first token far below prompt_tokens / 360 tok/s), shown on the chat bubble.
  - If cloud models (e.g. the Claude API) are added later, the same order maps directly onto cache_control breakpoints: rules + templates | project memory | session summary + messages.
- [x] **S9 Planner backend interface**: `PlannerBackend.chat(messages, schema) → (text, stats)`; session / memory / cache logic is independent of the backend. This stage implements `lmstudio` (with load / TTL / prewarm) and a generic `openai_compatible` (base_url + api_key + model), paving the way for stage 6.
- [x] **R2 Unmeasured video tiers** (user feedback, 2026-10-26): when the planner picks an unmeasured hd tier for video, no longer fail; instead queue with a measured tier automatically and offer a "try hd too (unmeasured)" button; the rules file says "video defaults to measured tiers".
- [x] **R1 Review decisions can be changed (requested by the user, 2026-10-26)**: a decided round / task can "change the review decision" and go back to review; the old decision goes into the decision history; if it was locked into the baseline pack, a new version restores that role to its previous asset (versions are never rewritten); rounds derived from the old decision stay, with a hint that /cancel is available. Console button + `/reopen`.
- [x] **I1 Bilingual console (requested by the user, 2026-10-26)**: all UI text goes through a dictionary (en / zh-CN), **English by default**, switch in the top-right corner, the choice is remembered in the browser; new config `language` (default `en`) as the server-side default.
- [x] **I2 Bilingual server-side text**: state names, system replies, Telegram messages and error hints follow `language`; the planner replies in the language of the user's message (agreed in the rules file).
- [x] **I3 Docs default to English**: external-facing docs (README, runbook, install docs, progress page) are English first, with Chinese versions alongside (`*.zh-CN.*`, the console switches with the language). **Internal docs (PLAN, baseline, design doc) stay in Chinese, not translated** (user decision, 2026-10-26). (Superseded 2026-11-12: the user asked for English main versions of these too, with the Chinese kept as `*.zh-CN.*`.)
- [x] **S7 Merged with B2, B3**: when a round awaits review, typing counts as a note on that round by default; the planner's reply restates "the points I understood" and cites the memories it used.

**Acceptance**: open two sessions in one project; in the second say "make one in the style we settled on", and the planner gets the style from project memory; after a long session is summarised automatically, "#2 of the previous round" still resolves correctly.

## 6c-3. Hand-tuning workflows in the console (proposed by the user, 2026-10-26)

Two layers, from safe to flexible:
- [x] **W1 Parameter editor (no LLM)**: press "manual tweak" on any task / candidate in the console to open that task's parameter form (template slots: prompt, seed, steps, reference images, tier, ... with range validation); submitting creates a new round of candidates, the DAG links to the original task, recorded as "manual tweak". The change is deterministic, equivalent to `/round`.
- [x] **W2 Open the full workflow (advanced, design doc L4 exploration)** (done 2026-10-26): implemented as "export the API workflow → drag it onto the ComfyUI canvas (the frontend's loadApiJson keeps node IDs) → Export (API) → import". Verified: ComfyUI's workflow list only accepts the canvas format, so **nothing is written into ComfyUI's user directory**; the diff is against the exported task, validation includes dangling links; the trial uses the graph's own values + the lowest tier; promotion writes into `data/templates/` and only marks the trial-run tier as measured. Measured: steps 4→6 + prompt change, trial at preview tier passed in 10.8 s. The original draft:
  - "Open in ComfyUI": export the workflow the task actually submitted (`data/jobs/<ID>/prompt.json` already exists) and copy it into ComfyUI's workflow list (`user/default/workflows/aco_lw/` subdirectory, existing workflows untouched), then edit nodes and links by hand on the ComfyUI canvas. **To verify**: whether the current ComfyUI frontend can load the API format directly; otherwise the canvas format must be saved too.
  - "Import back into aco_lw": import the edited workflow as a **candidate template variant** → `/object_info` graph validation → trial at the lowest tier → promoted to a real template after the user confirms (pluggable, same as the model registry). Unpromoted variants are only available inside that project.
  - The console also offers a read-only node / parameter view (JSON + slot highlighting) for comparison.

## 6c-3b. Stage 2.8: agent runtime (tool calls) + Executor interface + dynamic resource scheduling (raised by the user, 2026-10-26)

**References**: vhotseat (wandermyz/vhotseat: a local LLM and ComfyUI take turns on the same GPU / memory — gate before loading, never interrupt running work, batch submissions to reduce hand-overs, read state live); mature agents' tool loops (OpenAI / Anthropic tool-calling protocols, max steps, human-approval interrupts, resumable checkpoints).
**Measured premise (2026-10-26)**: native tool calling works with qwen3.8-27b on LM Studio (it calls `get_status` before `queue_round`); the prefix cache still hits after a tool round trip (second turn, 1235 tokens, first token 0.64 s).

### A. Agent runtime: the LLM emits tool calls, the orchestrator executes, results go back when resources allow
- **Loop**: LLM → tool_calls → orchestrator executes (internal tools / later MCP) → results stored → *the scheduler decides when to hand the results back to the LLM* → LLM continues → … → final reply.
- **Run states (persisted, resumable)**: `running` → `executing_tools` → `waiting_resources` (results ready, but memory is tight / a video is running, so they are parked) → `waiting_event` (waiting for a render) → `waiting_user` (needs your approval or answer) → `done` / `failed` / `cancelled`. Every step (the assistant's tool_calls, each tool result) is stored before continuing; after a daemon restart the run continues from the last step.
- **Tool classes**:
  - Read-only (run immediately, cacheable within a turn): `get_status`, `list_rounds`, `get_round`, `list_templates`, `get_pack`, `list_memories`, `view_candidates` (later: look at images with the model's vision).
  - Actions (via Studio, with idempotency key = run+step+call_id, never executed twice on recovery): `queue_round`, `tweak`, `redo_round`, `reject_round`, `propose_memory`.
  - Long tasks: `wait_for_round` → the run enters `waiting_event`; when the render finishes, an event wakes it with the result (outputs, duration, failure reason).
  - Needs approval (enters `waiting_user`, console / Telegram buttons): `select_and_lock` (unless you authorised it explicitly in that message), promoting a workflow, unmeasured video tiers; later `install_lora` (L5).
  - External (stage 7): `web_search` etc., behind a privacy switch.
- **Result hand-back policy**: handing back to the LLM is itself a job in the scheduler (LLM inference), timed by resources; overly long results are truncated / summarised before handing back, the original stays in the database.
- **Budget**: max steps and max render minutes per run; stop and ask you when exceeded.
- **Cache**: the assistant's tool_calls and tool results are stored and replayed verbatim (same as S8).
- **Compatibility**: the existing single-shot JSON planning stays as the "simple mode" backend; commands-only mode is unchanged.

### B. Executor interface (replacing the orchestrator's direct dependency on ComfyUI)
- `Executor`: `estimate(task)` (expected peak memory, duration, required resident model, switch cost) · `submit` · `poll` (progress) · `collect` · `cancel` (only our own) · `release` (release models) · `resident()` (what is resident now) · `health()`.
- Implementations: `ComfyExecutor` (existing logic moved over, behaviour unchanged), `FFmpegExecutor` (concatenate, grab frames, transcode, local subprocess); later: a standalone LTX service, other local generators, online generation services (no local memory).
- The LLM is also a workload: `load / unload / in_flight / resident`, scheduled by the resource manager together with the executors (vhotseat's driver).

### C. Dynamic resource scheduler (replacing `memgate`'s fixed thresholds)
- **Live resource view**: available memory, pressure level, swap trend; what each workload has resident (whether the LLM is loaded, which model family ComfyUI loaded last).
- **Estimates learned from history**: per (template, tier), moving averages of recorded metrics — peak memory, duration, load time; without history, fall back to the model registry.
- **One job queue**: render jobs and LLM inference jobs queue together; priority: user interaction > agent continuation > background exploration / trials; aging by waiting time prevents starvation.
- **Fewer switches**: jobs of the same model family run back to back (e.g. 4 images in a row with no LLM inference in between); switch costs (LLM load ~15 s + cache invalidation, image model cold start +12 s, video model load) count in the ordering.
- **Admission and release plan**: admit only if expected available (currently available + releasable) ≥ need + margin; release by plan when needed (`/free`, unload the LLM), **never interrupting running work** (wait for the LLM to finish answering; do not preempt a render).
- **ETA**: the console shows the queue timeline and expected completion times; a resource timeline chart (memory, pressure, who is using it).

**Decided (2026-10-26)**: the agent may check status, queue images, redo, refine per notes, wait for renders and continue to the next step on its own; **picking, locking into the baseline pack and using unmeasured video tiers need your approval** (buttons), unless you authorised it explicitly in that message. The tool-calling agent is the default mode; `/mode simple` switches back to single-shot JSON planning.

### D. Policy layer (added by the user, 2026-10-26)
**Principle**: what pauses is the agent conversation, not inference in progress; multiple tool calls must get their results filled in one by one, in order, with the original call IDs. **Explicit policy decides "whether" and "when" results go back to the LLM**; automatic retries, tier downgrades and rollbacks must be written in the policy file, and the runtime may not change what a task means on its own — anything the policy does not cover goes back to the LLM.

| Tool result | Next step (policy config, `policies.yaml`) |
|---|---|
| Render succeeded and belongs to a pipeline | Queue the next step directly, without the LLM |
| Executor queue congested / not enough memory | The scheduler waits |
| OOM | By policy: retry N times with the same parameters (explicit), or downgrade the tier (only if the policy lists allowed tiers) |
| Video finished, needs evaluation | Schedule a visual evaluation (later: a vision model; now: hand it to you / the LLM) |
| Evaluation failed | Back to the LLM to re-plan the prompt |
| You make a new request | Priority goes up; wait for a suitable model execution window |

**Execution policy**: each tool's permission (allow / needs approval / deny), parameter limits (e.g. max candidates per round), work directory, network access; shell / MCP / network tools are denied by default and whitelisted when enabled.

**MVP mapping**: runtime core = SQLite (sessions / tasks / tool results / events) · task scheduler (priority, dependencies, retries, wake conditions) · resource manager (memory pressure, GPU, concurrency) · LLM adapters (LM Studio / Codex / cloud APIs) · tool registry (ComfyUI / shell / MCP) · execution policy (permissions, parameters, directories, network).

### Tasks
- [x] **E1** Executor interface + `ComfyExecutor` (refactor, behaviour unchanged, all tests pass) + LLM workload
- [ ] **E2** `FFmpegExecutor` (frame grabbing, concatenation, transcoding)
- [x] **R1** Resource view + estimates learned from historical metrics (peak, duration, load time)
- [x] **Q1** One job queue and scheduler (priority, batching by model family, switch cost, admission and release plan, never interrupting running work), replacing the Worker main loop and memgate
- [x] **A1** Tool registry (JSON schema, classes, idempotency, approvals) + persistence of runs and steps
- [x] **A2** Agent loop: tool call execution, parking results, handing back to the LLM via the scheduler, event wake-ups, budget, crash recovery
- [ ] **A3** Channels: the console and Telegram show run steps (which tools were called, results, what it is waiting for) and approval buttons
- [ ] **U1** Console: queue timeline + ETA, resource timeline chart, run details
- [ ] **P1** Continuation policy (`policies.yaml`): by tool result type decide hand back to the LLM / auto next step / bounded retry / wait / notify you
- [ ] **P2** Execution policy: tool permissions, parameter limits, approval rules, directories and network; checked before every tool runs
- [ ] **D1** Pipeline tool `queue_pipeline`: multi-step sequential execution (`@prev` references the previous step's output), advancing between steps by policy without waking the LLM; paves the way for segmented long videos (FreeVideo continue_from + FFmpeg concatenation)
- [ ] **T1** Tests: fake Executor / fake LLM covering races (an LLM request during a render, a render arriving while the LLM answers, restart recovery, duplicate-execution protection)
- **Acceptance G2.8**: one-line request → the agent checks status, queues a round of images, waits for the render, then decides the next step with the result (e.g. make a video); while the video renders, the agent's continuation is parked and resumes automatically when the render ends; no critical memory pressure and no duplicate submits throughout; after a daemon restart midway, the run still completes.

## 6c-4. Stage 6 (draft): online models and external agents (Codex / Claude Code, ...)

**Principle unchanged**: the LLM / agents only produce data or call aco_lw's whitelisted commands; every side effect stays in the orchestrator. External agents get neither ComfyUI nor a shell (design doc concern #4).

Two integration points:
1. **Planner backend** (passive): `openai`, `anthropic` APIs, or non-interactive agent CLIs (`codex exec` / `claude -p`, forced JSON-schema output, read-only sandbox, no tools). Same interface as S9.
2. **Operator** (active): aco_lw provides an **MCP server** exposing only tools like `round / review / status / release / project / memory`; Codex, Claude Code etc. drive the creative loop as clients (optionally reviewing by looking at images). The agents' tool whitelist contains only these MCP tools.

| Impact | Handling |
|---|---|
| Memory: no need to unload the local 27B before video (saves 19.5 GiB) | The model registry's `exclusive_llm` applies only to the local backend |
| Latency: no load, no local prefill | Keep the S8 order, mapped onto cloud cache breakpoints (Anthropic `cache_control`; OpenAI automatic prefix caching) |
| Cost: billed per token | Per-project / per-day budget caps; beyond them, fall back to local or commands only; call records include usage and cost |
| Privacy: requests, project memory and reference images go to the cloud | Per-project switches "allow cloud" and "allow sending images"; off by default |
| Safety: agent CLIs come with file / shell abilities | Read-only sandbox + MCP tools only; all output schema-validated |
| Offline / failures | Fall back to the local 27B automatically, then to commands-only mode |

**Tasks**:
- [x] O1 Anthropic backend (native tool calls, Claude turns replayed unchanged, cache breakpoints, fallback on server-side refusals) + a generic OpenAI-compatible backend (2026-10-27)
- [x] O2 aco_lw MCP server (stdio, exposes only aco_lw tools, calls are forwarded to the daemon to execute)
- [x] O3 Delegating to Claude Code / Codex: Claude Code with `--tools ""` + only the aco_lw MCP; Codex in a read-only sandbox; empty work directory
- [x] O4 (partial) Fallback config: `llm_fallback` order + `allow_online` privacy switch (off by default); used when local resources are short or unavailable. To do: per-project choice, budget caps, settings page
- [ ] O5 Visual review (optional: an online model writes difference notes on candidates, still never auto-eliminates)
**Acceptance G6**: switching one project between local and online backends loses no session or project memory; falls back automatically when offline; agents cannot go beyond the MCP whitelist.

## 6c-5. Stage 7 (draft, raised by the user 2026-10-26, for later): online material and LoRAs

**Current state**: the planner is not an agent that can call tools itself; it only outputs one constrained JSON; no internet, no tools at all.

Uses: find reference images, look up descriptions of styles and shot language, read about models / LoRAs → choose → install → experiment.

- [ ] **N1 Read-only search tool**: the planner gets a `search` action (returns query terms), the orchestrator searches on its behalf (needs a search service and key), results come back as data and the planner decides; a controlled two-step loop, the number of rounds limited by budget. Request text is sent to the search service (privacy switch).
- [ ] **N2 Collecting reference images**: images in search results can be saved as `@upload` references in one step, with the source link; for reference editing / style exploration.
- [ ] **N3 LoRA information and choice**: look up LoRA notes (trigger words, recommended weights, base-model compatibility) and compile a candidate list for the user to choose from.
- [ ] **N4 LoRA install (design doc L5: human confirmation every time)**: download into ComfyUI's `models/loras/` only after the user confirms, verifying hash and size; this breaks the current "no model downloads" hard rule, so CLAUDE.md would then change to "LoRAs may be downloaded with the user's confirmation each time".
- [ ] **N5 LoRA experiments**: use W2's variant mechanism to create a workflow variant "with a LoRA node" → comparison trials at the lowest tier (with / without LoRA, different weights) → promoted after the user confirms.
- Implementation via one or both: stage 4 (OpenClaw sub-agent, reusing its search / skills) or stage 6 (online agents operating aco_lw over MCP).

## 6c-6. Stage 3 (revised): production pipeline — from novel to finished film (raised by the user, 2026-10-27)

The user's words: given a passage of a novel, plan a storyboard script, cast the main characters and locations, break down shots and camera-move details, plan video and consistency, generate the videos, edit and merge into a finished film; each step must pass review before the next. Use ArcReel as a reference, but **focus on infrastructure and extensibility**, use existing skills for internal features where possible, and aim for extensibility and effective scheduling.

### Conclusions from studying ArcReel (AGPL-3.0: borrow ideas only, no code)
- **Authoritative server-side plan**: `get_workflow_plan` returns steps / next_action / per-operation admission; the agent keeps no state machine of its own and does not judge progress from files or chat memory. → We do the same, and our plan is computed by deterministic code.
- **Artifact freshness**: each artifact type is current / stale / missing; changing an upstream only makes its dependants stale. → Same, decided by input fingerprints (revision hashes).
- **The main agent only orchestrates**: long source text never enters the main agent's context; sub-agents / skills read the files themselves, only paths and parameters are passed. → Same: the skill executor reads artifact paths.
- **Per-step confirmation protocol**: continue / redo (with change notes) / skip. → Reuse the existing review (pick / redo / reject / reopen).
- **The LLM only produces the visual layer, with strong schema constraints; violations land as drafts instead of being discarded and regenerated**. → Skill output is always JSON-Schema-validated; failures are stored as `*.invalid.json` + a list of violations.
- Where we differ: local-first + memory scheduling (it is mostly online APIs); workflows are data (YAML), not six mode combinations hard-coded in code and profiles.

### Architecture (5 new generic parts; the novel is just one workflow among others)
1. **Workflow spec (`workflows/*.yaml`)**: a stage DAG. Each stage declares `kind` (skill / render / tool), input artifacts, output artifact type and schema, `fan_out` (expand per character / location / shot), `gate` (review / auto, overridable by policy), and the skill or template and tier it uses. Adding "ads" or "music videos" = a new YAML + a few skills, no code change.
2. **Artifact store**: typed, versioned artifacts with lineage (input fingerprints) — script, character cards, location settings, shot list, keyframes, shot videos, edit timeline, final film. Change one character → keyframes / shots depending on it are marked stale automatically; old versions are kept for rollback. Reuses the existing SQLite and `data/`.
3. **Plan engine (authoritative, deterministic)**: computes from spec + artifacts each stage's state (completed / ready / active / review / blocked / stale), `next_action`, per-operation admission and the whole-film ETA. New agent tools `get_production_plan` / `run_stage` / `review_stage`; the console, Telegram and Claude Code all see the same plan.
4. **SkillExecutor (a new implementation of the Executor interface)**: loads Claude Code-format `SKILL.md` (frontmatter + instructions + references + optional scripts) from: built-in `skills/`, user `data/skills/`, optional external directories (e.g. `~/.claude/skills`). The stage spec names the skill and output schema; the executor is chosen by policy: the local 27B (short input) or delegation to Claude Code / Codex (long source text, complex adaptation), both only through aco_lw MCP tools. Scripts inside a skill only run in the orchestrator's sandbox (read-only inputs, write to the artifact directory, no network).
5. **FFmpegExecutor (E2)**: trim, concatenate, transitions, speed changes, burnt-in subtitles, audio mixing, last-frame extraction; CPU resource class, can run alongside renders. The edit comes from a skill as a timeline (EDL JSON); FFmpeg only executes the timeline — the LLM never assembles command lines directly.

### Scheduling (extending the existing ResourceManager)
- Stage tasks carry a resource class: `llm-local` / `llm-online` / `render-image` / `render-video` / `cpu`.
- **Batch by model family**: within a stage, finish all keyframes (FLUX) before switching to video (H3), avoiding back-and-forth model swaps; `cpu` (FFmpeg) skips the gate and can run alongside renders.
- Fanned-out shots are prioritised by order in the film + review waits: the first few shots come out first for you to review, the rest keep rendering while you review (review does not block later renders, but a reject withdraws queued tasks that depend on it).
- The whole-film ETA uses learned exec times: e.g. 8 shots × smoke 3 s (measured 329 s each) ≈ 44 min of video rendering + keyframes 8 × 27 s (measured, standard tier) ≈ 4 min (the totals are **estimates**).

### Novel → finished film workflow (the first YAML)
| # | Stage | Kind | Artifact | Review |
|---|---|---|---|---|
| 1 | Import source text | tool | source (text) | — |
| 2 | Adaptation: synopsis, scene breakdown | skill | adaptation.json | review |
| 3 | Bible: characters / locations / props / art style | skill | bible.json | review |
| 4 | Casting: character sheets, location reference images (n candidates each) | render (fan-out) | character / scene sheets | pick and lock |
| 5 | Storyboard script: per shot framing, composition, camera move, duration, lines, which settings it references | skill | shotlist.json | review |
| 6 | Keyframes (with casting images as reference) | render (fan-out) | keyframe per shot | pick |
| 7 | Shot videos (I2V, camera moves in the motion prompt) | render (fan-out) | clip per shot | pick / redo |
| 8 | Edit: order, trims, transitions, pacing | skill → timeline | timeline.json + rough cut | review |
| 9 | Compose the final film (optional subtitles / music) | tool (FFmpeg) | final.mp4 | review |

### Consistency plan (tiered)
- Possible now: the bible goes into project memory; each character's locked sheet + fixed seed; keyframes use `flux2-edit` with the character sheet as reference; shot videos are I2V from the picked keyframe; consecutive shots continue from the previous shot's **last frame** (new `@task:<id>#last`).
- Gaps: `flux2-edit` currently takes a single reference image; referencing "character + location" together needs a multi-reference workflow — you would need to provide it from 标准实验室 (we do not install nodes); face / style consistency scoring comes later.

### Milestones
- [x] **3a Infrastructure** (done 2026-10-27: `production.py` plan engine and artifact store, `skills.py`, `executors/ffmpeg.py`, `workflows/novel-to-film.yaml` + 4 schemas + 4 built-in skills, 5 agent tools, web API, the console's "Productions" view; 9 tests): workflow spec + artifact store (versions / staleness) + plan engine + SkillExecutor + FFmpegExecutor + generic stage review; tested with a 2-stage toy workflow and fake executors.
- **3b Novel → finished film**: the first YAML + built-in skills (adaptation, bible, storyboard, edit); run a short story at smoke tier (asking for confirmation before real renders).
- [~] **3c The console's "Productions" view** (minimal version done: stage pipeline, item thumbnails, start / approve / redo / reject / keep / edit JSON by hand, final film playback; storyboard board and timeline preview to do): stage DAG (reusing Flow), storyboard board (shots × keyframes / videos), timeline preview, per-stage review; Telegram pushes each stage review.
- **3d Consistency improvements**: multi-reference templates (waiting for your workflow), last-frame continuation, voice-over / TTS (to be discussed).

### 3.1 Multi-source input + your own casting images (raised by the user 2026-10-27; done the same day: `ingest.py`, tool stages, alternatives, provide, console and agent entries; measured a Wikipedia page + YouTube metadata / subtitles / thumbnail / 2 frames in 4.9 s)
Goal: a production's input is not just a passage of a novel — it can be a natural-language description + images, web links, YouTube links; your own casting photos can be used directly.
1. **Input is a set of sources**: `text` (a novel or a one-line idea), `image` (with name / role: character · location · style · prop, and usage: use as-is / redraw in style), `url` (page body + main image), `youtube` (title, description, chapters, subtitles, thumbnail, optionally N frames).
2. **New stage kind `tool`**: deterministic orchestrator tools (registrable, extensible). New `brief` stage (tool: ingest) turns all sources into one "material pack" (text + reference image list + source status); later skills only read the material pack. Fetching happens in the orchestrator (timeouts, size limits, page scripts never run); failed sources are marked with the reason and do not block the others.
3. **Your own casting images**: the bible attaches a `reference` (a reference image ID in the material pack) to characters / locations; render stages support conditional branches written in data (`alternatives: when … → provide / render`):
   - use as-is → directly an approved casting image (no rendering);
   - redraw in style → FLUX.2 edits a casting image using your photo as reference.
   In addition, any render item can be replaced by hand with "use my image" (console item menu / API), counted as approved; downstream goes stale by dependency as usual.
4. **Entry points**: creating a production in the console accepts multi-line links + multiple image uploads (name, type and usage per image); the agent tool `create_production` accepts `sources`; photos sent in Telegram are already uploads and can be referenced directly.
5. Dependencies: `trafilatura` (page body), `yt-dlp` (YouTube metadata / subtitles / thumbnail / low-res video for frame grabs). No model downloads.

### 3.2 Consistency and smart ComfyUI control (user decision 2026-10-27)
Background: in the "Miss Malfoy" test film, neither keyframes nor videos kept the casting face, and two-person shots mixed up features. Root causes: video relied only on "first frame + one sentence", H3 prompts did not follow the official format, FLUX had a single reference.
Decisions:
1. **Video consistency through FreeVideo character cards + films**: a new FreeVideo executor (local `/freevideo/agent/v1`; the token is read only in-process, never printed). Characters / locations → FreeVideo character cards (up to 6 labelled images + voice); shot videos → a FreeVideo multi-shot film (plan → start → takes per shot → picked in our review → continuations use the last frame of the previous shot's picked take → finish). Pause / review stay under our control (a film's pause can only be operated by the user in the FreeVideo page: while we are paused we stop advancing choose / finish).
2. **The agent's ComfyUI permissions**: may explore models, nodes and your workflows, build and modify workflows, trial-run at the lowest tier (through the orchestrator, subject to pause and the resource gate); **installing nodes / downloading models still needs your approval**.
3. **Connect artokun/comfyui-mcp, but scheduled by our orchestrator**: the orchestrator starts it as an MCP client (stdio) and registers its tools by class in our tool table and policy: reads (model / node / workflow queries) allowed directly; building (editing graphs, validation) allowed; execution calls are turned into our tasks submitted by the orchestrator (trackable progress, subject to pause and resource scheduling); installs need approval.
4. **Install MiniMax's official `h3-prompt-writing` skill** (data/skills); video prompts follow the official H3 format (English sections, `<d>[English] …</d>` lines, speaker IDs, shot-change times).
5. This film is all in English (British English).
Order: H3 skill + English → FreeVideo character cards (Malfoy from your 7 HD images) → FreeVideo film executor for the shot video stage → comfyui-mcp scheduled by the orchestrator → dual-reference keyframes.

- 2026-10-27, at the user's request: FreeVideo's approval switch `ComfyUI/output/FreeVideo/.agent/confirm.json` is set to `{"start": false, "reshoot_seconds": null}` (shooting and reshooting no longer ask in the FreeVideo page; our "start" button and review stay as they are).

### Decided (user, 2026-10-27)
1. Skill sources: built-in + external skills compatible with the Claude Code SKILL.md format (`data/skills/`, mapped per stage); `~/.claude/skills` is not scanned by default.
2. LLM for adaptation / storyboard: configurable (per stage), local 27B by default; when video holds the local one, fall back per `llm_fallback`.
3. The first finished film is silent + optional subtitles (FFmpeg burns in the lines); music / voice-over moves to 3d.

## 6c-7. General agent (user decision 2026-11-07; done, branch `agent-general-tools`)

Architecture in `docs/design.html` §6b. The LLM is the brain and only proposes tool calls; the orchestrator executes every tool (generation tools + generic tools in `toolbox.py` + MCP in `mcp_hub.py`) and hands results back as memory allows. Replaces the original "OpenClaw sub-agent provides search / skills / MCP" (stage 4).

- [x] Generic tools: web_fetch, run_command (each approved, dangerous operations refused), file read / write, skills (including `~/.claude/skills`), agent memory, add_media, send_file
- [x] MCP: stdio + Streamable HTTP; read-only tools run directly, others need approval; currently configured: Exa
- [x] Delegating to the FreeVideo agent: `/fv`, `delegate_freevideo` (Claude Code + FreeVideo MCP, approved each time)
- [x] Z-Image templates (`zimage-t2i` / `zimage-i2i`), the default for casting (Klein 4B's face shape is not controllable, measured over 4 batches)

Gaps to Claude Code / OpenClaw in everyday use (see design doc §6b) and next steps:
- [x] Vision: the `look` tool (the local 27B is a vision model); images go to the LLM right after the tool result, converted automatically for the Claude backend
- [x] Approval friction: an "Always allow" button stores a rule (`cmd:ffmpeg`, `mcp:…`, `delegate:codex`) + `/permissions bypass` (hard refusals still apply; installing nodes / downloading models still asks every time)
- [x] Online brains: `/model local|openai|openrouter|anthropic|claude_code|codex` (needs `allow_online`; turning it off falls back to local automatically); `delegate_agent` hands a sub-task to Claude Code / Codex (approved each time)
- [ ] Controlled self-extension: the agent proposes a new template → the orchestrator validates + trial-runs at the lowest tier → registered after the user approves

## 6d. Stage 5 (draft): installable package + setup wizard (goal: one step to get running on a new machine)

**Goal**: on a new Mac, run one command from the install docs → open the setup wizard in the browser → when it is done, images / videos work.

**Decided (2026-10-26)**: Apple Silicon Macs only; the installer installs only aco_lw; ComfyUI / FreeVideo / LM Studio / models are **detected + guided** by the wizard (commands or download links, recognised automatically once installed); the form is **one command + a launchd background service**; the setup wizard is **web-based (`/setup` on first visit, the console's "Settings" page afterwards)**.

**Scope**
- **Installer**: one-command install (`curl … | sh` → check macOS / Apple Silicon / memory / disk → install uv → install aco_lw into `~/Applications/aco_lw` or as a `uv tool`) → register a launchd service (start at login, restart on crash) → open the wizard.
- **Setup wizard (web, `/setup` automatically on first start)**:
  1. Environment check: memory, disk, chip, ports in use (avoid occupied ports automatically).
  2. ComfyUI: detect an existing instance / enter an address; check required custom nodes and model files, listing whatever is missing.
  3. Models and templates: enable the templates matching the installed models (pluggable, video models can change); unmeasured tiers are marked unverified, and a smoke measurement can be run in the wizard and written back to the model registry.
  4. LLM: detect LM Studio / choose a model / test one load and one plan; or choose "commands-only mode" without an LLM.
  5. Telegram (optional): enter the token → verified automatically → send `/whoami` to join the whitelist in one step.
  6. Preferences such as data directory, candidates per round, memory margin → written to `config.toml`.
  7. Done: run a test image, show a success page.
- **Settings page**: after the wizard, the same configuration can be changed any time in the console's "Settings" (including re-running the checks).
- **Install docs**: `docs/install.md` (prerequisites, one-step install, wizard screenshots, uninstall, upgrade, FAQ).
- **Uninstall / upgrade**: `aco_lw uninstall` (keep or delete data), `aco_lw upgrade`.
- **Acceptance G5**: on a clean Mac user account, following only the install docs, the first image within 30 minutes (model downloads excluded); the service recovers automatically after a reboot.

## 7. Directory layout (target)

```
video-agent/
├── PLAN.md                 # this file
├── docs/design.html        # the original design doc
├── docs/baseline.md        # G0 measurement conclusions (stage 0 output)
├── bench/                  # stage 0 measurement scripts
├── templates/<name>/       # api.json + slots.yaml
├── src/video_agent/        # comfy / registry / schema / render / store / memgate / scheduler / recover / cli
├── tests/
├── config.example.toml     # ComfyUI / LM Studio addresses, margins, release policy
└── data/                   # gitignored: sqlite, jobs/<id>/manifest.json + outputs, bench CSVs
```

## 8. Decided / open

Decided (2026-10-26):
1. The T0.5 video smoke test may run; **the VM stays on**, so the baseline is the everyday state.
2. The user will use the ComfyUI web page by hand while the system runs → confirm the queue twice before releasing, and only release models this system loaded.
3. The video model is not fixed (MiniMax H3 / LTX 2.5 / newer) → pluggable registry; this round's smoke test uses the installed FreeVideo (MiniMax H3, 04 text-to-video).
4. Web progress console on port 8790 (8765 is another server of the user's).
6. Stage 5: Apple Silicon only; the installer installs only aco_lw, heavy dependencies are detected + guided by the web wizard; one command + launchd; web wizard + settings page.
5. Stage 2: the user creates a dedicated Telegram bot, token / chat_id go into the gitignored `config.toml`; **candidates are sent as original image files** (characters are fictional, fine to go to the cloud); **the phone only uses Telegram**, the review console is only used on this machine (still bound to 127.0.0.1); candidates per round are configurable, default 4.

Open:
- FreeVideo adapts to memory → the memory gate's criterion should change from "enough or not" to "available memory vs duration curve", decided by the T0.5 / T0.6 data.
- Swap in use before measuring was 12.7 / 14.3 GB (2026-10-26); the baseline should note it.
- Daily task volume, whether to batch overnight, whether the LLM is needed — decide after G1.
