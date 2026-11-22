"""Bilingual catalogue of server-side text (I2). English by default; `config.language = "zh"` switches to Chinese.

Covers only text the user sees (chat replies, system notices, Telegram help and buttons). Logs and code comments are not translated.
Chat history keeps the language it was written in; switching languages does not rewrite history.
"""

from __future__ import annotations

_LANG = "en"

M: dict[str, tuple[str, str]] = {
    # General
    "ack": ("Got it — thinking…", "收到，正在理解……"),
    "media_held": (
        "Got it ({n} attachment(s)). Tell me what to do with it — your next message within {m} min carries it along.",
        "收到（共 {n} 个附件）。告诉我要用它做什么，{m} 分钟内你的下一条消息会带上它。",
    ),
    "agent_parked": (
        "Queued: {why}. I'll pick this up automatically as soon as memory frees up; /status works meanwhile (no LLM needed).",
        "已排队：{why}。内存一空出来就自动接着处理；期间 /status 随时可查（不需要 LLM）。",
    ),
    "agent_resumed": (
        "Memory is free — working on your message now…",
        "内存空出来了，开始处理你的消息……",
    ),
    "park_film": (
        "a FreeVideo film is shooting and needs the memory",
        "FreeVideo 正在拍片，占着内存",
    ),
    "park_render": ("a video render is holding the memory", "视频正在生成，占着内存"),
    "park_pressure": ("memory pressure is critical", "内存压力过高"),
    "llm_queued_video": (
        "A video is rendering and holding memory; the LLM will reply right after it finishes.",
        "视频生成占着内存，LLM 先排队，生成结束后马上处理。",
    ),
    "cannot": ("Couldn't do that: {e}", "没能处理：{e}"),
    "planner_error": ("Planner error: {e}", "规划器出错：{e}"),
    "not_done": ("Not done: {e}", "操作没有执行：{e}"),
    "unknown_button": ("Unknown button {d}", "未知按钮 {d}"),
    "unknown_cmd": (
        "Unknown command. /help lists all commands.",
        "不认识这个命令。/help 查看全部命令。",
    ),
    "command_only": (
        "Command-only mode: text won't wake the LLM. Use /round, or /mode on_demand.",
        "当前是纯命令模式，文字不会唤醒 LLM。用 /round 直接出图，或 /mode on_demand。",
    ),
    "invalid_params": ("Invalid parameters: {e}", "参数无效：{e}"),
    # ComfyUI tool approvals
    "comfy_approval": (
        "The agent wants to run a ComfyUI action that changes your install: {t} {a}\n{why}",
        "agent 想执行一个会改动 ComfyUI 安装的操作：{t} {a}\n{why}",
    ),
    # Productions
    "stage_review": ("🎬 {p}: {s} is ready for review", "🎬 {p}：{s} 待审核"),
    "stage_invalid": (
        "🎬 {p}: {s} — the model's output failed validation; edit it or redo",
        "🎬 {p}：{s} 的输出未通过校验，请手改或重做",
    ),
    "film_needs_approval": (
        "🎬 {p}: FreeVideo asks you to approve starting the film — open ComfyUI (http://127.0.0.1:8188) → Extensions → FreeVideo, and approve the card in its agent chat; filming starts within a minute.",
        "🎬 {p}：FreeVideo 要你批准开拍——打开 ComfyUI（http://127.0.0.1:8188）→ 顶部菜单 Extensions → FreeVideo，在它的 agent 对话里点批准卡片；一分钟内开拍。",
    ),
    "stage_failed": ("🎬 {p}: {s} failed", "🎬 {p}：{s} 失败"),
    # Planner results
    "queued_round": (
        "Queued: {n} candidate(s) ({tpl} / {tier})",
        "已排队：{n} 个候选（{tpl} / {tier}）",
    ),
    "queued_new_round": ("Queued a new round: {n} candidate(s)", "已排队新一轮：{n} 个候选"),
    "queued_refine": (
        "Queued a new round: {n} candidate(s) (keeping #{idx}'s seed)",
        "已排队新一轮：{n} 个候选（保留 #{idx} 的种子）",
    ),
    "tier_fallback": (
        "Tier “{asked}” hasn't been measured for video yet (memory and time unknown), so I queued it at “{used}”. Want to try “{asked}” as well?",
        "视频档位「{asked}」还没实测过（内存与耗时未知），先按「{used}」出。要不要也试一下「{asked}」？",
    ),
    "btn_try_tier": ("Also try {t} (unmeasured)", "也试一下 {t}（未实测）"),
    "try_tier_queued": (
        "Queued at tier {t} (unmeasured — first run, watch memory): {n} candidate(s)",
        "已按 {t} 档排队（未实测——首次运行，注意内存）：{n} 个候选",
    ),
    "agent_tier_approval": (
        "Tier “{t}” of {tpl} hasn't been measured; estimated ~{m} min (scaled from measured tiers). Run it?",
        "{tpl} 的「{t}」档还没实测过，估算约 {m} 分钟（按已实测档位折算）。要跑吗？",
    ),
    "agent_select_approval": (
        "Proposed: select #{idx} of “{t}”{r}. {why}",
        "建议：选中「{t}」的 #{idx}{r}。{why}",
    ),
    "btn_approve": ("✅ Approve", "✅ 同意"),
    "btn_decline": ("✕ No", "✕ 不要"),
    "delete_busy": (
        "{n} task(s) are still queued or running; cancel them first.",
        "有 {n} 个任务还在排队或生成中，先取消再删。",
    ),
    "delete_in_production": (
        "Used by the active production “{p}”; finish or delete that production first.",
        "进行中的制片「{p}」还在用它；先结束或删除那个制片。",
    ),
    "prod_delete_busy": (
        "{n} of this production's render(s) are still queued or running; pause it with “stop now” first.",
        "这个制片还有 {n} 个渲染在排队或生成中；先「暂停 → 立即停止」再删。",
    ),
    "no_round": ("No round {r}.", "没有这一轮：{r}。"),
    "no_task": ("No task {t}.", "没有这个任务：{t}。"),
    "log_deleted": (
        "Deleted {rounds} round(s), {tasks} task(s) and their files ({freed_mb} MB).",
        "已删除 {rounds} 轮、{tasks} 个任务及其文件（{freed_mb} MB）。",
    ),
    "log_kept": (
        " Kept {n} item(s) still used by production(s): {p}.",
        "保留了 {n} 项进行中制片仍在使用的：{p}。",
    ),
    "btn_always": ("✅ Always ({r})", "✅ 始终允许（{r}）"),
    "agent_auto_approved": ("⚡ Auto-approved ({how}): {what}", "⚡ 已自动批准（{how}）：{what}"),
    "perm_how_bypass": ("bypass mode", "bypass 模式"),
    "perm_how_rule": ("always-allow rule", "始终允许规则"),
    "perm_now": (
        (
            "Permission mode: {m}\nAlways-allowed:\n{rules}\n"
            "/permissions bypass — stop asking · /permissions default — ask each time · "
            "/permissions forget <rule|all>"
        ),
        (
            "权限模式：{m}\n始终允许：\n{rules}\n"
            "/permissions bypass 不再审批 · /permissions default 逐条审批 · "
            "/permissions forget <规则|all> 删除规则"
        ),
    ),
    "perm_no_rules": ("(none)", "（无）"),
    "status_bypass": (
        "\n⚡ Permissions: bypass (agent does not ask)",
        "\n⚡ 权限：bypass（agent 不再请求批准）",
    ),
    "perm_set_bypass": (
        (
            "Bypass mode on: the agent no longer asks before commands, MCP calls, hand-overs or "
            "unmeasured video tiers (installing nodes / downloading models still asks). Hard refusals (ComfyUI :8188, FreeVideo token, killing "
            "processes, LLM load/unload) and the online switch still apply. /permissions default to go back."
        ),
        (
            "已开 bypass：agent 执行命令、MCP 调用、交给外部 agent、未实测的视频档位都不再问你（装节点 / 下模型仍会问）。"
            "硬拒绝（直连 ComfyUI :8188、读 FreeVideo token、结束进程、加载 / 卸载 LLM）和在线开关照旧。"
            "/permissions default 恢复逐条审批。"
        ),
    ),
    "perm_set_default": (
        "Back to asking each time (always-allow rules still apply).",
        "已恢复逐条审批（“始终允许”的规则仍然生效）。",
    ),
    "perm_forgot": ("Removed {n} rule(s).", "已删除 {n} 条规则。"),
    "agent_step_budget": (
        "I've used my step budget ({n} steps) for this request. Tell me how to continue.",
        "这次请求的步数预算（{n} 步）用完了。告诉我接下来怎么做。",
    ),
    "agent_already_decided": ("That was already decided.", "这个已经决定过了。"),
    "agent_declined": ("OK — not doing that.", "好的，不做。"),
    "agent_approved": ("Approved.", "已同意。"),
    "fv_offline": (
        "Handing over to the FreeVideo agent uses Claude Code (online); turn on freevideo_agent_online (or allow_online) in config.toml first.",
        "交给 FreeVideo agent 要用 Claude Code（在线）；先在 config.toml 打开 freevideo_agent_online（或 allow_online）。",
    ),
    "agent_delegate_approval": (
        "Hand this sub-task to {agent} (online)? It reports back here.\n{task}",
        "把这个子任务交给 {agent}（在线）？它做完会在这里汇报。\n{task}",
    ),
    "agent_online_off": (
        "Online models / agents are off: set allow_online = true in config.toml first.",
        "在线模型 / agent 没开：先在 config.toml 设 allow_online = true。",
    ),
    "model_now": (
        "Agent brain: {m}{note}\nOptions: {opts}\nSwitch with /model <name>.",
        "当前 agent 大脑：{m}{note}\n可选：{opts}\n用 /model <名字> 切换。",
    ),
    "model_set": ("Agent brain switched to {m}.", "agent 大脑已切换为 {m}。"),
    "model_unknown": ("Unknown brain {m}. Options: {opts}", "没有 {m} 这个选项。可选：{opts}"),
    "model_local_note": (
        " (online is off, so local is used)",
        "（在线没开，实际用本地）",
    ),
    "fv_delegate_approval": (
        "Hand this task to the FreeVideo agent (Claude Code + FreeVideo tools)?\n{task}",
        "把这个任务交给 FreeVideo agent（Claude Code + FreeVideo 工具）？\n{task}",
    ),
    "fv_started": (
        "Handed to the FreeVideo agent (Claude Code). It reports back here; its videos are in FreeVideo's library.",
        "已交给 FreeVideo agent（Claude Code）。它做完会在这里汇报；视频在 FreeVideo 的资料库里。",
    ),
    "agent_mcp_approval": (
        "The agent wants to call MCP tool {tool} with:\n```\n{args}\n```{why}",
        "agent 想调用 MCP 工具 {tool}，参数：\n```\n{args}\n```{why}",
    ),
    "agent_cmd_approval": (
        "The agent wants to run a command:\n```\n{cmd}\n```{why}",
        "agent 想执行命令：\n```\n{cmd}\n```{why}",
    ),
    "agent_cmd_estimate": (
        "Expected memory: ~{g} GiB{x} ({src}); it starts when that much is free.",
        "预计占用内存：约 {g} GiB{x}（{src}）；内存够了才开始执行。",
    ),
    "agent_cmd_exclusive": (
        ", exclusive (the LLM is unloaded meanwhile)",
        "，独占（期间卸载 LLM）",
    ),
    "style_set": ("Planner style: {s}", "规划方式：{s}"),
    "locked_as": ("Locked as “{r}”", "已锁定为「{r}」"),
    "ask_lock": ("Lock it into the baseline pack?", "要锁进基准包吗？"),
    "selected_ask_lock": (
        "Selected #{idx}. Lock it into the baseline pack?",
        "已选中 #{idx}。要锁进基准包吗？",
    ),
    "pack_locked": (
        "Baseline pack v{v}: locked “{r}”. Reference it later as @pack:{r}.",
        "基准包 v{v}：已锁定「{r}」。之后可用 @pack:{r} 引用。",
    ),
    "refine_ack": (
        "Tweak #{idx}: {note}. Translating the note into parameters…",
        "收到微调 #{idx}：{note}。正在把批注翻译成参数……",
    ),
    "reject_ack": ("Rejected. Adjusting per your note: {note}", "已打回。正在按批注调整：{note}"),
    "ask_refine_note": (
        "Send one line describing the tweak for #{idx} (e.g. softer eyes, side light)",
        "请发一句对 #{idx} 的微调说明（例如：眼神更忧伤，光打侧面）",
    ),
    "ask_reject_note": (
        "Send one line: why rejected / what to change",
        "请发一句打回原因 / 改进方向",
    ),
    "redo_queued": ("Redo: queued {n} new candidate(s)", "整轮重做：已排队 {n} 个新候选"),
    # Memory
    "mem_offer": (
        "💡 Save this to the project memory?\n“{t}”",
        "💡 要把这条记进项目记忆吗？\n「{t}」",
    ),
    "mem_remember": ("✅ Remember", "✅ 记住"),
    "mem_ignore": ("Ignore", "忽略"),
    "mem_accepted": ("Remembered: {t}", "已记住：{t}"),
    "mem_ignored": ("Ignored: {t}", "已忽略：{t}"),
    "mem_added": ("Remembered (#{id}): {t}", "已记住（#{id}）：{t}"),
    "mem_archived": ("Archived #{id}: {t}", "已归档 #{id}：{t}"),
    "mem_list": (
        "Project memory:\n{items}{pending}\n/memory add TEXT · /memory del ID",
        "项目记忆：\n{items}{pending}\n/memory add 文本 · /memory del 编号",
    ),
    "mem_pending": (
        "\n{n} proposal(s) awaiting confirmation (see Projects & Chat)",
        "\n待确认 {n} 条（控制台“项目与对话”里处理）",
    ),
    "empty": ("(empty)", "（空）"),
    # Commands
    "paused": (
        "Paused: the running task finishes, queued ones won't start. /resume to continue.",
        "已暂停：正在跑的任务会跑完，排队的不再开始。/resume 恢复。",
    ),
    "resumed": ("Resumed.", "已恢复出图。"),
    "cancelled": ("Cancelled {n} queued task(s){x}", "已取消 {n} 个排队任务{x}"),
    "cancelled_running": (" and interrupted the running one", "，并中断了正在跑的本系统任务"),
    "mode_now": ("Current mode: {m}\nOptions: {opts}", "当前模式：{m}\n可选：{opts}"),
    "mode_set": ("Switched to: {m}", "已切换到：{m}"),
    "no_projects": ("No projects yet", "还没有项目"),
    "project_now": ("Current project: {n} ({s})", "当前项目：{n}（{s}）"),
    "project_new": ("Created and switched to “{n}”", "已新建并切换到「{n}」"),
    "project_use": ("Switched to “{n}”", "已切换到「{n}」"),
    "project_done": (
        "Project “{n}” marked complete; history is kept.",
        "项目「{n}」已标记完成，历史永久保留。",
    ),
    "project_usage": (
        "Usage: /project [new NAME | use NAME | done]",
        "用法：/project [new 名称 | use 名称 | done]",
    ),
    "session_new": (
        "New session “{t}”. Project memory carries over; earlier chat doesn't.",
        "已新开会话「{t}」。项目记忆会带过来，之前的对话不会。",
    ),
    "session_use": ("Switched to session “{t}”", "已切换到会话「{t}」"),
    "llm_list": (
        "Local models (✅ loaded, ← in use by aco):\n{rows}\n/llm use NAME|N · /llm load · /llm unload",
        "本地模型（✅ 已加载，← aco 在用）：\n{rows}\n/llm use 名称|序号 · /llm load · /llm unload",
    ),
    "llm_switching": (
        "Switching to {m} (needs ~{g} GiB, estimate)… I'll tell you when it's loaded.",
        "正在切换到 {m}（约需 {g} GiB，估算）……加载好了告诉你。",
    ),
    "llm_ready": ("{m} is loaded ({s} s).", "{m} 已加载（{s} 秒）。"),
    "llm_already": ("{m} is already loaded.", "{m} 已经加载着。"),
    "llm_unloaded": ("Unloaded {m}.", "已卸载 {m}。"),
    "session_deleted": (
        "Deleted session “{t}” ({m} messages). Current session: “{c}”",
        "已删除会话「{t}」（{m} 条消息）。当前会话：「{c}」",
    ),
    "session_delete_q": (
        "Delete session “{t}” ({n} messages, with the agent's history in it)? This cannot be undone.",
        "删除会话「{t}」（{n} 条消息，连同其中的 agent 记录）？删除后无法恢复。",
    ),
    "btn_delete": ("🗑 Delete", "🗑 删除"),
    "btn_keep": ("Keep", "保留"),
    "btn_pressed": ("[button] {b}", "［按钮］{b}"),
    "session_busy": (
        "The agent is still working in this session ({n} run(s)); cancel it first",
        "agent 还在这个会话里干活（{n} 个运行），先取消再删",
    ),
    "session_list": (
        "Sessions (1 = most recent):\n{rows}\n/session new [TITLE] · /session use N · /session delete N",
        "会话（1 = 最近活跃）：\n{rows}\n/session new [标题] · /session use 序号 · /session delete 序号",
    ),
    "session_row": ("{i}. {t} · {n} msgs{cur}", "{i}. {t} · {n} 条{cur}"),
    "current_mark": (" ← current", " ← 当前"),
    "no_pack": (
        "No baseline pack yet. Approve a candidate and lock it.",
        "当前项目还没有基准包。选中候选后可锁定。",
    ),
    "pack_info": ("Baseline pack v{v}: {items}", "基准包 v{v}：{items}"),
    "no_rounds": ("No rounds in this project yet", "当前项目还没有轮次"),
    "no_review": ("Nothing to review", "没有待审阅的轮次"),
    "review_no_tg": (
        "{n} round(s) to review; Telegram isn't configured — use the console #review page.",
        "有 {n} 轮待审阅；未配置 Telegram，请在控制台 #review 页审阅。",
    ),
    "review_pushed": ("Re-sent {n} round(s) for review.", "已重新推送 {n} 轮待审阅候选。"),
    "round_usage": (
        "Usage: /round TEMPLATE [COUNT] [prompt]; templates: {t}",
        "用法：/round 模板 [数量] [prompt]；模板：{t}",
    ),
    "status": (
        "Project: {p}\nRendering: {r}{paused}, queued {q}\nMemory: {m} GiB free, pressure {pr}\nLLM: {llm}",
        "项目：{p}\n出图：{r}{paused}，排队 {q}\n内存：可用 {m} GiB，压力 {pr}\nLLM：{llm}",
    ),
    "status_agent": (
        "\nAgent: {a} working, {w} queued for the LLM",
        "\nAgent：处理中 {a}，等 LLM 排队 {w}",
    ),
    "status_llm_loaded": (" (loaded)", "（已加载）"),
    "status_llm_unloaded": (" (unloaded)", "（未加载）"),
    "status_films": ("\nFilm shooting: {n} (LLM held back)", "\n正在拍片：{n} 部（LLM 暂不加载）"),
    "status_prod": ("\nProduction “{t}”: {stage} — {c}", "\n制片《{t}》：{stage} — {c}"),
    "status_running": ("running {t}", "正在跑 {t}"),
    "status_idle": ("idle", "空闲"),
    "status_paused": (" (paused)", "（已暂停）"),
    # Notifications
    "round_ready": (
        "“{t}”: {ok}/{n} candidate(s) ready for review",
        "「{t}」{ok}/{n} 个候选待审阅",
    ),
    "round_failed": (
        "“{t}”: every candidate failed — see the console.",
        "「{t}」这一轮全部失败，请看控制台。",
    ),
    "round_ready_tg": ("“{t}”: {n} candidate(s) to review", "「{t}」{n} 个候选待审阅"),
    "whoami": ("Your chat_id: {id}", "你的 chat_id：{id}"),
    "error": ("Something went wrong: {e}", "出错了：{e}"),
    # Buttons
    "btn_select": ("✅ Pick {i}", "✅ 选{i}"),
    "btn_refine": ("🔧 Tweak {i}", "🔧 微调{i}"),
    "btn_redo": ("🎲 Redo round", "🎲 整轮重做"),
    "btn_reject": ("↩️ Reject + note", "↩️ 打回+批注"),
    "btn_no_lock": ("Don't lock", "不锁定"),
    "roles": ("character,scene,style", "角色,场景,风格"),
    # Studio log entries
    "log_select": ("Selected #{idx}{lock}", "选中 #{idx}{lock}"),
    "log_lock_suffix": (", locked as “{r}”", "，锁定为「{r}」"),
    "log_redo": ("Redo whole round (new seeds)", "整轮重做（换种子）"),
    "log_refine": ("Tweak #{idx}: {note}", "微调 #{idx}：{note}"),
    "log_reject": ("Rejected: {note}", "打回：{note}"),
    "log_no_note": ("(no note)", "（无批注）"),
    "log_tweak": ("Manual tweak of {id}: {changed}", "手动微调 {id}：{changed}"),
    "log_no_change": ("no parameter changes", "无参数改动"),
    "log_pack": ("Baseline pack v{v}: locked “{r}”", "基准包 v{v}：锁定「{r}」"),
    "log_project_new": ("Created and switched to project “{n}”", "新建并切换到项目「{n}」"),
    "log_project_done": ("Project “{n}” marked complete", "项目「{n}」已标记完成"),
    "log_summary": (
        "Long session: folded {n} earlier exchange(s) into the summary",
        "会话较长，已把 {n} 轮较早往来压缩进摘要",
    ),
    "log_reopen": ("Reopened for review", "重新审阅"),
    "log_reopen_round": (
        "Changed decision: “{t}” is back to review",
        "更改审阅结果：「{t}」回到待审阅",
    ),
    "log_unlock": ("Undo lock of “{r}” (decision changed)", "撤销锁定「{r}」（审阅结果已更改）"),
    "err_not_reopenable": (
        "Only decided rounds/tasks (approved, rejected, redo, tweak) can be reopened.",
        "只有已决定（通过 / 打回 / 重做 / 微调）的轮次或任务可以重新审阅。",
    ),
    "reopened": ("Back to review: “{t}”.{c}", "已回到待审阅：「{t}」。{c}"),
    "reopen_children": (
        " {n} round(s) spawned by the old decision are still running — /cancel if not needed.",
        "旧决定派生的 {n} 轮仍在生成，不需要可 /cancel。",
    ),
    "history_session": ("History", "历史会话"),
    "session_n": ("Session {n}", "会话 {n}"),
    "default_project": ("Default project", "默认项目"),
}

HELP = (
    """aco_lw commands (no LLM needed):
/status system and queue status
/fv <task> hand a video task to the FreeVideo agent (Claude Code + FreeVideo tools)
/model [local|openai|openrouter|anthropic|claude_code|codex] show / switch the agent's brain
/permissions [default|bypass|forget <rule|all>] approval mode and always-allow rules
/rounds recent rounds
/round TEMPLATE [COUNT] [prompt]  queue a round directly
/review re-send rounds waiting for review
/redo redo the latest round under review
/reopen [ROUND_ID] change the decision of the latest (or given) decided round
/pause /resume pause / resume rendering
/cancel cancel queued tasks (and interrupt our running one)
/mode [on_demand|session|always|command_only] LLM residency
/project · /projects · /project new NAME · /project use NAME · /project done
/session · /session new [TITLE] · /session use N · /session delete N
/llm · /llm use NAME · /llm load · /llm unload
/memory · /memory add TEXT · /memory del ID
/pack current baseline pack
/whoami show your chat_id
Anything else: a request or note — wakes the LLM.""",
    """aco_lw 命令（不唤醒 LLM）：
/status 系统与队列状态
/fv <任务> 把视频任务交给 FreeVideo agent（Claude Code + FreeVideo 工具）
/model [local|openai|openrouter|anthropic|claude_code|codex] 查看 / 切换 agent 的大脑
/permissions [default|bypass|forget <规则|all>] 审批模式与“始终允许”规则
/rounds 最近几轮
/round 模板 [数量] [prompt]  直接出一轮候选
/review 重新推送待审阅的候选
/redo 最近一轮待审阅的整轮重做
/reopen [轮ID] 更改最近一轮（或指定轮）的审阅结果
/pause /resume 暂停 / 恢复出图
/cancel 取消排队中的任务（含正在跑的本系统任务）
/mode [on_demand|session|always|command_only] LLM 驻留模式
/project · /projects · /project new 名称 · /project use 名称 · /project done
/session · /session new [标题] · /session use 序号 · /session delete 序号
/llm · /llm use 名称 · /llm load · /llm unload
/memory · /memory add 文本 · /memory del 编号
/pack 当前基准包
/whoami 显示 chat_id
其它文字：新需求或批注，会唤醒 LLM 理解。""",
)

STATE = {
    "draft": ("queued", "排队"),
    "validated": ("validated", "已校验"),
    "waiting_memory": ("waiting memory", "等待内存"),
    "submitted": ("submitted", "已提交"),
    "running": ("running", "生成中"),
    "review": ("to review", "待审阅"),
    "done": ("done", "完成"),
    "rejected": ("rejected", "打回"),
    "failed": ("failed", "失败"),
    "timeout": ("timed out", "超时"),
    "generating": ("generating", "生成中"),
    "closed": ("decided", "已决定"),
    "active": ("active", "进行中"),
    "archived": ("archived", "已归档"),
}

MODE = {
    "on_demand": ("on demand (auto-unload when idle)", "按需唤醒（空闲自动卸载）"),
    "session": ("session-resident (until switched back)", "会话常驻（直到切回按需）"),
    "always": ("always resident", "始终常驻"),
    "command_only": ("command-only (never wakes the LLM)", "纯命令（不唤醒 LLM）"),
}


def set_language(lang: str) -> None:
    global _LANG
    _LANG = "zh" if str(lang).lower().startswith("zh") else "en"


def language() -> str:
    return _LANG


def _pick(pair: tuple[str, str]) -> str:
    return pair[1] if _LANG == "zh" else pair[0]


def tr(key: str, **kw) -> str:
    return _pick(M[key]).format(**kw)


def help_text() -> str:
    return _pick(HELP)


def state(s: str) -> str:
    return _pick(STATE[s]) if s in STATE else s


def mode(m: str) -> str:
    return _pick(MODE[m]) if m in MODE else m


def roles() -> list[str]:
    return tr("roles").split(",")
