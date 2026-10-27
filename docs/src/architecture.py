"""生成 docs/architecture.html（英文）与 docs/architecture.zh-CN.html（中文）。

图里的文字都在 T（中英对照）里，形状只画一次：改图只改这里，然后运行
    uv run python docs/src/architecture.py
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

T = {
    # ---- 页面 ----
    "title": ("aco_lw Architecture", "aco_lw 架构"),
    "updated": ("Updated 2026-10-27 · phase 2.8 (agent runtime, executors, resource scheduler, policies, online fallback)",
                "更新于 2026-10-27 · 阶段 2.8（agent 运行时、执行端、资源调度、策略层、在线回退）"),
    "other": ('<a href="architecture.zh-CN.html">中文</a>', '<a href="architecture.html">English</a>'),
    "lead": ("One rule shapes everything: <b>only the orchestrator has side effects</b>. Channels, the LLM and external agents produce data or call fixed tools; resources are checked right before anything loads or submits, and nothing that is already running is interrupted.",
             "一条原则决定了整体结构：<b>只有编排器产生副作用</b>。各渠道、LLM 和外部 agent 只产出数据或调用固定的工具；资源在任何东西加载或提交之前检查，已经在运行的工作绝不打断。"),
    # ---- 图 1 ----
    "f1": ("1. System architecture", "1. 系统架构"),
    "f1c": ("Who talks to whom. Every path to ComfyUI goes through the orchestrator and an executor; the resource manager sits between the agent and the worker and decides when either may use memory. Dashed: optional / fallback paths.",
            "谁和谁通信。所有通向 ComfyUI 的路径都经过编排器和执行端；资源管理器位于 agent 与 Worker 之间，决定双方何时可以占用内存。虚线：可选 / 回退路径。"),
    "console": ("Web console :8790", "Web 控制台 :8790"),
    "console_s": ("review · chat · queue · workflows", "审阅 · 对话 · 队列 · workflow"),
    "tg": ("Telegram bot", "Telegram bot"),
    "tg_s": ("whitelist · buttons · photos", "白名单 · 按钮 · 发图"),
    "cli": ("va CLI", "va 命令行"),
    "cli_s": ("calls Studio / Orchestrator directly", "直接调用 Studio / 编排器"),
    "ext": ("Claude Code / Codex", "Claude Code / Codex"),
    "ext_s": ("own login + online model · no shell", "自己的登录与在线模型 · 无 shell"),
    "actions": ("Actions (channel-agnostic)", "Actions（与渠道无关）"),
    "actions_s": ("commands & buttons run directly · free text → runtime · every message logged",
                  "命令与按钮直接执行 · 自由文本 → 运行时 · 每条消息落库"),
    "mcp": ("aco_lw MCP server (stdio)", "aco_lw MCP 服务（stdio）"),
    "mcp_s": ("only aco_lw tools → POST /api/agent/tool", "只有 aco_lw 工具 → POST /api/agent/tool"),
    "runtime": ("Agent runtime", "Agent 运行时"),
    "runtime_s1": ("tool loop · park & resume · approvals", "工具循环 · 暂存与恢复 · 审批"),
    "runtime_s2": ("runs + exact messages in SQLite", "运行与逐字消息存 SQLite"),
    "policy": ("policies.yaml", "policies.yaml"),
    "policy_s1": ("execution: allow · limits · approvals", "执行：允许 · 上限 · 审批"),
    "policy_s2": ("continuation: resume · auto-next · retry", "续跑：交回 · 自动下一步 · 重试"),
    "lms": ("LM Studio (local 27B)", "LM Studio（本地 27B）"),
    "lms_s": ("on demand · TTL · prefix cache", "按需加载 · TTL · 前缀缓存"),
    "anth": ("Anthropic API", "Anthropic API"),
    "anth_s": ("raw-content replay · caching", "原始内容回放 · 缓存"),
    "orr": ("OpenRouter / OpenAI-compat.", "OpenRouter / OpenAI 兼容"),
    "orr_s": ("online, step by step", "在线，逐步接手"),
    "studio": ("Studio", "Studio"),
    "studio_s1": ("projects · sessions · memory", "项目 · 会话 · 记忆"),
    "studio_s2": ("rounds · decisions · pack · reopen", "轮次 · 决定 · 基准包 · 重开"),
    "rm": ("Resource manager", "资源管理器"),
    "rm_s1": ("LLM gate · render gate", "LLM 闸门 · 渲染闸门"),
    "rm_s2": ("learned estimates · queue policy · ETA", "学习的估计 · 排队策略 · ETA"),
    "worker": ("Worker (+ heartbeat)", "Worker（+ 心跳）"),
    "worker_s1": ("next draft by policy", "按策略取下一个任务"),
    "worker_s2": ("collects late results", "收回超时后才完成的结果"),
    "orch": ("Orchestrator", "编排器"),
    "orch_s": ("validate · job id saved before submit · poll · recover", "校验 · 先落库 job id 再提交 · 轮询 · 恢复"),
    "exec": ("Executor: ComfyExecutor", "执行端：ComfyExecutor"),
    "exec_s": ("/object_info check · /prompt · /history · /free", "/object_info 校验 · /prompt · /history · /free"),
    "planned": ("More executors (planned)", "更多执行端（规划中）"),
    "planned_s": ("FFmpeg · LTX service · online generators", "FFmpeg · LTX 服务 · 在线生成器"),
    "comfy": ("ComfyUI :8188", "ComfyUI :8188"),
    "comfy_s": ("FLUX.2 images · FreeVideo / MiniMax H3", "FLUX.2 出图 · FreeVideo / MiniMax H3"),
    "store": ("SQLite + data/ — tasks · rounds · runs · agent_msgs · sessions · memories · messages · jobs/ · packs/ · uploads/ · variants/",
              "SQLite + data/ —— 任务 · 轮次 · 运行 · agent 消息 · 会话 · 记忆 · 对话 · jobs/ · packs/ · uploads/ · variants/"),
    "a_ui": ("HTTP", "HTTP"),
    "a_tg": ("long poll", "长轮询"),
    "a_mcp": ("MCP tools/call", "MCP tools/call"),
    "a_ext": ("external_tool (same policies)", "external_tool（同样的策略）"),
    "a_free": ("free text", "自由文本"),
    "a_llm": ("chat + tools (gated)", "对话 + 工具（经闸门）"),
    "a_fb": ("fallback · allow_online", "回退 · allow_online"),
    "a_del": ("delegate whole request", "整个请求委托"),
    "a_tools": ("tools: queue_round · select · …", "工具：queue_round · select · …"),
    "a_admit": ("llm_admit?", "llm_admit?"),
    "a_resume": ("render done → resume", "渲染结束 → 恢复"),
    "a_gate": ("prepare_render", "prepare_render"),
    "a_drafts": ("create drafts", "创建任务"),
    "a_exec": ("execute(task)", "execute(task)"),
    "a_sub": ("submit · poll · collect", "提交 · 轮询 · 收集"),
    "a_http": ("HTTP", "HTTP"),
    "a_check": ("checks every tool call", "每次工具调用都校验"),
    # ---- 图 2 ----
    "f2": ("2. One request, end to end", "2. 一次请求的完整时序"),
    "f2c": ("A request that renders: while the render runs the conversation is parked in the database (no thread, no LLM in memory); when the round is ready the continuation policy decides whether the LLM is woken again.",
            "一次需要渲染的请求：渲染期间对话暂存在数据库里（不占线程，LLM 不在内存里）；这一轮出来后，由续跑策略决定要不要再唤醒 LLM。"),
    "l_you": ("You", "你"), "l_rt": ("Agent runtime", "Agent 运行时"), "l_rm": ("Resource mgr", "资源管理器"),
    "l_llm": ("LLM", "LLM"), "l_w": ("Studio / Worker", "Studio / Worker"), "l_c": ("ComfyUI", "ComfyUI"),
    "m1": ("“make a video of …”", "“做一段……的视频”"), "m2": ("ack", "回执“收到”"),
    "m3": ("llm_admit?", "llm_admit?"), "m4": ("ok", "可以"),
    "m5": ("messages + tools", "消息 + 工具"), "m6": ("tool_calls: queue_round", "tool_calls：queue_round"),
    "m7": ("queue_round → drafts", "queue_round → 排队"), "m8": ("result: round id, ETA", "结果：轮 ID、ETA"),
    "m9": ("wait_for_round", "wait_for_round"),
    "n1": ("parked: waiting_event", "暂存：waiting_event"),
    "m10": ("prepare_render", "prepare_render"),
    "n2": ("wait for answer · unload LLM", "等回答写完 · 卸载 LLM"),
    "m11": ("submit (job id saved first)", "提交（先落库 job id）"),
    "m12": ("done → collect outputs", "完成 → 收集产物"),
    "m13": ("on_round_ready", "on_round_ready"),
    "n3": ("continuation policy", "续跑策略"),
    "m14": ("candidates", "候选结果"), "m15": ("final text", "最终回复"),
    "m16": ("reply + approval buttons", "回复 + 审批按钮"),
    # ---- 图 3 ----
    "f3": ("3. Which LLM runs this step", "3. 这一步由哪个 LLM 来跑"),
    "f3c": ("Local first. When the local model can't run (a video holds the memory, or LM Studio fails), the configured fallback chain decides — and nothing leaves the machine unless allow_online is on.",
            "本地优先。本地模型跑不了时（视频占着内存，或 LM Studio 出错），按配置的回退链决定——不开 allow_online 就不会有任何内容离开本机。"),
    "d_need": ("a run needs the LLM", "运行需要 LLM"),
    "d_online": ("primary backend online?", "主后端是在线的？"),
    "d_use": ("use it (no local gate)", "直接用（无需本地闸门）"),
    "d_admit": ("llm_admit?", "llm_admit?"),
    "d_admit_s": ("no exclusive render here or in ComfyUI", "本进程与 ComfyUI 都没有独占渲染"),
    "d_local": ("local LM Studio", "本地 LM Studio"),
    "d_local_s": ("load with TTL · prefix cache", "带 TTL 加载 · 前缀缓存"),
    "d_fb": ("llm_fallback + allow_online?", "设了 llm_fallback 且 allow_online？"),
    "d_park": ("park: waiting_resources", "暂存：waiting_resources"),
    "d_park_s": ("resumes when the render ends", "渲染结束后恢复"),
    "d_api": ("online API continues this step", "在线 API 接着跑这一步"),
    "d_api_s": ("anthropic · openrouter · openai_compatible", "anthropic · openrouter · openai_compatible"),
    "d_cli": ("delegate the whole request", "整个请求委托出去"),
    "d_cli_s": ("claude_code · codex (fresh runs only)", "claude_code · codex（仅新请求）"),
    "yes": ("yes", "是"), "no": ("no", "否"), "api": ("API first", "先遇到 API"), "clin": ("CLI first", "先遇到 CLI"),
    # ---- 图 4 ----
    "f4": ("4. The two resource gates", "4. 两道资源闸门"),
    "f4c": ("Checked right before loading or submitting, never by interrupting running work. The LLM gate also asks ComfyUI itself, so a video submitted by another process (or by hand) blocks LLM loading too.",
            "在加载或提交之前检查，从不打断正在运行的工作。LLM 闸门还会直接问 ComfyUI，所以别的进程（或你手动）提交的视频同样会挡住 LLM 加载。"),
    "g_r": ("Render gate — before every submit", "渲染闸门——每次提交前"),
    "g_r1": ("estimate peak & time (per tier; adaptive engines use the registry)", "估计峰值与耗时（按档位；自适应引擎用注册表值）"),
    "g_r2": ("exclusive model? wait for the LLM's answer, then unload it", "独占模型？等 LLM 答完，再卸载"),
    "g_r3": ("switching model family? release the previous one", "换模型族？先释放上一族"),
    "g_r4": ("queue idle · free ≥ peak + margin · pressure < critical", "队列空闲 · 可用 ≥ 峰值 + 余量 · 压力未到严重"),
    "g_r5": ("else: /free → unload LLM → wait ≤ 10 min → time out", "否则：/free → 卸载 LLM → 等待 ≤ 10 分钟 → 超时"),
    "g_r6": ("mark render_active → submit", "标记 render_active → 提交"),
    "g_l": ("LLM gate — before every local load or step", "LLM 闸门——每次本地加载或推理前"),
    "g_l1": ("exclusive render active in this process?", "本进程有独占渲染在跑？"),
    "g_l2": ("exclusive job queued / running in ComfyUI? (any process)", "ComfyUI 里有独占任务在排 / 在跑？（任何进程）"),
    "g_l3": ("memory pressure critical?", "内存压力严重？"),
    "g_l4": ("all no → admit · any yes → refuse (the caller parks, never blocks)", "全否 → 放行 · 任一为是 → 拒绝（调用方暂存，不阻塞）"),
    "g_l5": ("render_done → listeners resume parked runs", "render_done → 通知者恢复被暂存的运行"),
    # ---- 图 5 ----
    "f5": ("5. Agent run states", "5. Agent 运行的状态"),
    "f5c": ("A run is a conversation, not a thread: every wait is a row in the database, so the daemon can restart at any point and continue.",
            "一次运行是一段对话，不是一个线程：每种等待都是数据库里的一行，daemon 随时重启都能接着走。"),
    "s_run": ("running", "running 处理中"),
    "s_wr": ("waiting_resources", "waiting_resources 等资源"),
    "s_we": ("waiting_event", "waiting_event 等渲染"),
    "s_wu": ("waiting_user", "waiting_user 等你"),
    "s_del": ("delegated", "delegated 已委托"),
    "s_done": ("done", "done 完成"), "s_fail": ("failed", "failed 失败"), "s_can": ("cancelled", "cancelled 取消"),
    "t_start": ("your message", "你的消息"),
    "t_wr": ("gate refused, no fallback", "闸门拒绝，无回退"), "t_wr2": ("render done", "渲染结束"),
    "t_we": ("wait_for_round / pipeline", "wait_for_round / 流水线"), "t_we2": ("round ready (policy)", "这轮完成（按策略）"),
    "t_wu": ("approval / ask_user", "审批 / 提问"), "t_wu2": ("button or answer", "按钮或回答"),
    "t_del": ("fallback = CLI", "回退到 CLI"), "t_del2": ("CLI finished", "CLI 完成"),
    "t_done": ("final text", "最终回复"), "t_fail": ("LLM error", "LLM 出错"), "t_can": ("/cancel · Stop", "/cancel · 停止"),
    "t_loop": ("tool results ready → next step", "工具结果齐了 → 下一步"),
    # ---- 图 6 ----
    "f6": ("6. Continuation policy", "6. 续跑策略"),
    "f6c": ("What happens when a render finishes. Retries repeat the exact parameters and seeds; anything policies.yaml doesn't cover goes back to the LLM.",
            "渲染完成后走哪条路。重试只按完全相同的参数和种子；policies.yaml 没写到的情况一律交回 LLM。"),
    "c_res": ("render result arrives", "渲染结果到达"),
    "c_cls": ("classify the round", "判定这一轮"),
    "c_ok": ("succeeded", "成功"), "c_oom": ("failed: OOM", "失败：OOM"), "c_other": ("failed: lost / other", "失败：丢失 / 其他"),
    "c_next": ("auto_next — queue next pipeline step", "auto_next——排下一步流水线"),
    "c_next_s": ("no LLM involved", "不唤醒 LLM"),
    "c_llm": ("resume_llm — hand results back", "resume_llm——把结果交回 LLM"),
    "c_llm_s": ("LLM reviews or re-plans", "由 LLM 审阅或重新规划"),
    "c_retry": ("retry — same parameters & seeds", "retry——同参数同种子重试"),
    "c_retry_s": ("max_retries from policy, no LLM", "次数见策略，不唤醒 LLM"),
    "c_more": ("pipeline, more steps", "流水线还有下一步"), "c_last": ("single round / last step", "单轮 / 最后一步"),
    "c_left": ("retries left", "还有重试次数"), "c_none": ("no retries left", "重试用完"),
    # ---- 图 7 ----
    "f7": ("7. Task states", "7. 任务的状态"),
    "f7c": ("Every transition is checked and logged. A timeout only stops watching (the job keeps running and is collected later); a decided task can be reopened.",
            "每次迁移都经校验并记录。超时只是停止观察（任务继续跑，之后会被收回）；已决定的任务可以重新打开。"),
    "k_draft": ("draft", "排队"), "k_val": ("validated", "已校验"), "k_wm": ("waiting_memory", "等待内存"),
    "k_sub": ("submitted", "已提交"), "k_run": ("running", "生成中"), "k_rev": ("review", "待审阅"),
    "k_done": ("done", "完成"), "k_rej": ("rejected", "打回"), "k_fail": ("failed", "失败"), "k_to": ("timeout", "超时"),
    "k_reopen": ("reopen (change decision)", "重开（更改审阅结果）"),
    "k_late": ("late result collected", "超时后收回结果"),
    "k_err": ("invalid · cancelled · executor error · lost", "校验失败 · 取消 · 执行端出错 · 丢失"),
    "k_gate": ("gate timeout / monitor timeout", "门控超时 / 观察超时"),
    # ---- 组件表 ----
    "tbl": ("Components and where they live", "组件与代码位置"),
    "th": (("Component", "Code", "Responsibility"), ("组件", "代码", "职责")),
}

ROWS = [
    ("Actions", "daemon.py", ("Channel-agnostic entry: commands, buttons, free text; logs every message", "与渠道无关的入口：命令、按钮、自由文本；每条消息落库")),
    ("Agent runtime", "agent.py", ("Tool loop, park/resume, approvals, delegation, external_tool", "工具循环、暂存与恢复、审批、委托、external_tool")),
    ("Policies", "policy.py · policies.yaml", ("Execution and continuation rules; user overrides in data/policies.yaml", "执行与续跑规则；用户覆盖在 data/policies.yaml")),
    ("Planner", "planner.py", ("Cache-friendly context, sessions & memory, simple JSON mode", "缓存友好的上下文、会话与记忆、简单 JSON 模式")),
    ("LLM backends", "backends.py", ("LM Studio, OpenAI-compatible, OpenRouter, Anthropic", "LM Studio、OpenAI 兼容、OpenRouter、Anthropic")),
    ("Agent CLIs + MCP", "agent_cli.py · mcp_server.py", ("Claude Code / Codex with only aco_lw tools", "只带 aco_lw 工具的 Claude Code / Codex")),
    ("Studio", "studio.py", ("Projects, sessions, rounds, decisions, pack, memory, uploads", "项目、会话、轮次、决定、基准包、记忆、上传")),
    ("Resource manager", "resources.py", ("Gates, learned estimates, queue policy, ETA, history", "闸门、学习的估计、排队策略、ETA、资源曲线")),
    ("Orchestrator", "scheduler.py", ("Create, execute, monitor, recover; the only side-effect path", "创建、执行、监控、恢复；唯一的副作用路径")),
    ("Executors", "executors/", ("ComfyExecutor today; FFmpeg / LTX / online later", "目前 ComfyExecutor；以后 FFmpeg / LTX / 在线")),
    ("Workflows", "variants.py", ("ComfyUI round-trip: import, diff, trial, promote", "ComfyUI 往返：导入、对比、试跑、晋升")),
    ("Store", "store.py", ("SQLite schema, migrations, state machine", "SQLite 结构、迁移、状态机")),
    ("Web / Telegram", "web/ · telegram.py", ("Console UI + API; bot with whitelist", "控制台界面与 API；带白名单的 bot")),
]


class D:
    """极简 SVG 作图：所有线与字用 currentColor，唯一的强调色用 --accent。"""

    def __init__(self, w: int, h: int, label: str):
        self.w, self.h, self.label, self.parts = w, h, label, []

    def box(self, x, y, w, h, title, subs=(), acc=False, dashed=False, small=False):
        cls = "b" + (" acc" if acc else "") + (" dash" if dashed else "")
        self.parts.append(f'<g class="{cls}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/>')
        lines = [s for s in subs if s]
        ty = y + h / 2 - (len(lines) * 7) + (4 if not lines else 2)
        self.parts.append(f'<text class="{"ts" if small else "t"}" x="{x + w / 2}" y="{ty}" '
                          f'text-anchor="middle">{html.escape(title)}</text>')
        for i, s in enumerate(lines):
            self.parts.append(f'<text class="s" x="{x + w / 2}" y="{ty + 15 + i * 14}" '
                              f'text-anchor="middle">{html.escape(s)}</text>')
        self.parts.append("</g>")

    def arrow(self, pts, label=None, at=None, dashed=False, acc=False, anchor="middle", back=False):
        cls = "ln" + (" dash" if dashed else "") + (" accl" if acc else "")
        p = " ".join(f"{x},{y}" for x, y in pts)
        mark = "url(#ah-acc)" if acc else "url(#ah)"
        self.parts.append(f'<polyline class="{cls}" points="{p}" fill="none" marker-end="{mark}"/>')
        if label:
            if at is None:
                (x1, y1), (x2, y2) = pts[0], pts[-1]
                at = ((x1 + x2) / 2 + 6, (y1 + y2) / 2 - 5)
            self.parts.append(f'<text class="lbl" x="{at[0]}" y="{at[1]}" text-anchor="{anchor}">'
                              f'{html.escape(label)}</text>')

    def text(self, x, y, s, cls="lbl", anchor="middle"):
        self.parts.append(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{html.escape(s)}</text>')

    def raw(self, s):
        self.parts.append(s)

    def svg(self) -> str:
        defs = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                'markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="currentColor"/>'
                '</marker><marker id="ah-acc" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                'markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" class="accf"/>'
                '</marker></defs>')
        return (f'<svg viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{html.escape(self.label)}">'
                + defs + "".join(self.parts) + "</svg>")


def build(lang: int) -> str:
    t = lambda k: T[k][lang]
    figs = []

    # ---------- 图 1：系统架构 ----------
    d = D(1120, 810, t("f1c"))
    d.box(40, 20, 200, 56, t("console"), [t("console_s")])
    d.box(270, 20, 200, 56, t("tg"), [t("tg_s")])
    d.box(500, 20, 200, 56, t("cli"), [t("cli_s")])
    d.box(760, 20, 320, 56, t("ext"), [t("ext_s")], dashed=True)
    d.box(40, 130, 660, 56, t("actions"), [t("actions_s")])
    d.box(760, 130, 320, 56, t("mcp"), [t("mcp_s")], dashed=True)
    d.arrow([(140, 76), (140, 130)], t("a_ui"), at=(148, 106), anchor="start")
    d.arrow([(370, 76), (370, 130)], t("a_tg"), at=(378, 106), anchor="start")
    d.arrow([(920, 76), (920, 130)], t("a_mcp"), at=(928, 106), anchor="start", dashed=True)
    d.box(40, 240, 420, 70, t("runtime"), [t("runtime_s1"), t("runtime_s2")], acc=True)
    d.box(760, 240, 320, 70, t("policy"), [t("policy_s1"), t("policy_s2")])
    d.arrow([(250, 186), (250, 240)], t("a_free"), at=(258, 216), anchor="start")
    d.arrow([(920, 186), (920, 240)], t("a_ext"), at=(928, 216), anchor="start", dashed=True)
    d.arrow([(760, 262), (460, 262)], t("a_check"), at=(600, 255))
    d.raw('<rect class="grp" x="30" y="350" width="690" height="92" rx="10"/>')
    d.box(40, 366, 210, 64, t("lms"), [t("lms_s")])
    d.box(270, 366, 200, 64, t("anth"), [t("anth_s")], dashed=True)
    d.box(490, 366, 220, 64, t("orr"), [t("orr_s")], dashed=True)
    d.arrow([(145, 310), (145, 366)], t("a_llm"), at=(153, 342), anchor="start")
    d.arrow([(400, 310), (400, 366)], t("a_fb"), at=(408, 342), anchor="start", dashed=True)
    d.arrow([(440, 240), (440, 214), (730, 214), (730, 48), (760, 48)], t("a_del"), at=(724, 207),
            anchor="end", dashed=True)
    d.box(40, 480, 330, 70, t("studio"), [t("studio_s1"), t("studio_s2")])
    d.box(390, 480, 330, 70, t("rm"), [t("rm_s1"), t("rm_s2")], acc=True)
    d.box(760, 480, 320, 70, t("worker"), [t("worker_s1"), t("worker_s2")])
    d.arrow([(60, 310), (60, 480)], t("a_tools"), at=(68, 465), anchor="start")
    d.arrow([(330, 442), (330, 470), (430, 470), (430, 480)], t("a_admit"), at=(338, 465), anchor="start")
    d.arrow([(800, 550), (800, 568), (700, 568), (700, 550)], t("a_gate"), at=(806, 581), anchor="start")
    d.arrow([(700, 480), (700, 456), (740, 456), (740, 290), (460, 290)], t("a_resume"),
            at=(746, 440), anchor="start", acc=True)
    d.box(390, 590, 330, 60, t("orch"), [t("orch_s")])
    d.arrow([(205, 550), (205, 575), (420, 575), (420, 590)], t("a_drafts"), at=(213, 570), anchor="start")
    d.arrow([(920, 550), (920, 620), (720, 620)], t("a_exec"), at=(926, 600), anchor="start")
    d.box(390, 690, 330, 56, t("exec"), [t("exec_s")])
    d.box(40, 690, 330, 56, t("planned"), [t("planned_s")], dashed=True)
    d.box(760, 690, 320, 56, t("comfy"), [t("comfy_s")])
    d.arrow([(555, 650), (555, 690)], t("a_sub"), at=(563, 674), anchor="start")
    d.arrow([(720, 718), (760, 718)], t("a_http"), at=(740, 710))
    d.raw('<rect class="store" x="40" y="766" width="1040" height="32" rx="6"/>')
    d.text(560, 787, t("store"), cls="s")
    figs.append((t("f1"), d.svg(), t("f1c")))

    # ---------- 图 2：时序 ----------
    lanes = [("l_you", 70), ("l_rt", 250), ("l_rm", 430), ("l_llm", 600), ("l_w", 790), ("l_c", 980)]
    d = D(1060, 660, t("f2c"))
    for k, x in lanes:
        d.box(x - 70, 16, 140, 32, t(k), small=True)
        d.raw(f'<line class="life" x1="{x}" y1="48" x2="{x}" y2="650"/>')
    X = {k: x for k, x in lanes}

    def msg(y, a, b, key, dashed=False, acc=False):
        x1, x2 = X[a], X[b]
        off = 6 if x2 > x1 else -6
        d.arrow([(x1 + off, y), (x2 - off, y)], t(key), at=((x1 + x2) / 2, y - 6), dashed=dashed, acc=acc)

    def note(y, lane, key, w=200, acc=False):
        x = X[lane]
        d.box(x - w / 2, y - 14, w, 26, t(key), small=True, acc=acc)

    msg(85, "l_you", "l_rt", "m1"); msg(115, "l_rt", "l_you", "m2", dashed=True)
    msg(150, "l_rt", "l_rm", "m3"); msg(178, "l_rm", "l_rt", "m4", dashed=True)
    msg(213, "l_rt", "l_llm", "m5"); msg(241, "l_llm", "l_rt", "m6", dashed=True)
    msg(276, "l_rt", "l_w", "m7"); msg(304, "l_w", "l_rt", "m8", dashed=True)
    msg(332, "l_rt", "l_llm", "m5"); msg(360, "l_llm", "l_rt", "m9", dashed=True)
    note(392, "l_rt", "n1", w=210, acc=True)
    msg(430, "l_w", "l_rm", "m10"); note(458, "l_rm", "n2", w=230)
    msg(492, "l_w", "l_c", "m11"); msg(520, "l_c", "l_w", "m12", dashed=True)
    msg(552, "l_w", "l_rt", "m13", acc=True); note(580, "l_rt", "n3", w=170, acc=True)
    msg(610, "l_rt", "l_llm", "m14"); msg(632, "l_llm", "l_rt", "m15", dashed=True)
    msg(648, "l_rt", "l_you", "m16")
    figs.append((t("f2"), d.svg(), t("f2c")))

    # ---------- 图 3：选 LLM ----------
    d = D(1040, 470, t("f3c"))
    d.box(40, 20, 220, 44, t("d_need"), small=True)
    d.box(40, 110, 220, 48, t("d_online"), acc=True, small=True)
    d.box(320, 110, 220, 48, t("d_use"), small=True)
    d.box(40, 210, 220, 56, t("d_admit"), [t("d_admit_s")], acc=True)
    d.box(320, 210, 220, 56, t("d_local"), [t("d_local_s")])
    d.box(40, 320, 220, 48, t("d_fb"), acc=True, small=True)
    d.box(320, 320, 220, 56, t("d_park"), [t("d_park_s")])
    d.box(600, 270, 400, 56, t("d_api"), [t("d_api_s")], dashed=True)
    d.box(600, 380, 400, 56, t("d_cli"), [t("d_cli_s")], dashed=True)
    d.arrow([(150, 64), (150, 110)])
    d.arrow([(260, 134), (320, 134)], t("yes"), at=(290, 127))
    d.arrow([(150, 158), (150, 210)], t("no"), at=(158, 188), anchor="start")
    d.arrow([(260, 238), (320, 238)], t("yes"), at=(290, 231))
    d.arrow([(150, 266), (150, 320)], t("no"), at=(158, 296), anchor="start")
    d.arrow([(260, 344), (320, 344)], t("no"), at=(290, 337))
    d.arrow([(150, 368), (150, 414), (600, 414)], t("clin"), at=(380, 408))
    d.arrow([(150, 368), (150, 392), (570, 392), (570, 298), (600, 298)], t("api"), at=(576, 340), anchor="start")
    figs.append((t("f3"), d.svg(), t("f3c")))

    # ---------- 图 4：两道闸门 ----------
    d = D(1040, 440, t("f4c"))
    d.text(250, 24, t("g_r"), cls="t")
    d.text(790, 24, t("g_l"), cls="t")
    ys = [44, 104, 164, 224, 284, 354]
    for i, k in enumerate(["g_r1", "g_r2", "g_r3", "g_r4", "g_r5", "g_r6"]):
        d.box(20, ys[i], 460, 44, f"{i + 1}. {t(k)}", small=True, acc=(k == "g_r2"))
        if i:
            d.arrow([(250, ys[i - 1] + 44), (250, ys[i])])
    ly = [44, 104, 164, 244, 324]
    for i, k in enumerate(["g_l1", "g_l2", "g_l3", "g_l4", "g_l5"]):
        d.box(560, ly[i], 460, 44, t(k), small=True, acc=(k == "g_l2"))
        if 0 < i < 3:
            d.arrow([(790, ly[i - 1] + 44), (790, ly[i])], t("no"), at=(798, ly[i] - 6), anchor="start")
    d.arrow([(790, 208), (790, 244)], t("no"), at=(798, 236), anchor="start")
    d.arrow([(790, 288), (790, 324)])
    d.arrow([(480, 376), (520, 376), (520, 346), (560, 346)], "render_done", at=(500, 398),
            anchor="start", acc=True)
    figs.append((t("f4"), d.svg(), t("f4c")))

    # ---------- 图 5：运行状态 ----------
    d = D(1060, 400, t("f5c"))
    d.box(140, 160, 780, 50, t("s_run"), acc=True)
    d.arrow([(20, 185), (140, 185)], t("t_start"), at=(24, 177), anchor="start")
    for k, x, lab in (("s_can", 300, "t_can"), ("s_fail", 520, "t_fail"), ("s_done", 740, "t_done")):
        d.box(x, 30, 160, 44, t(k), small=True)
        d.arrow([(x + 80, 160), (x + 80, 74)], t(lab), at=(x + 88, 124), anchor="start")
    d.raw('<path class="ln accl" d="M170,160 C170,108 270,108 270,160" fill="none" marker-end="url(#ah-acc)"/>')
    d.text(220, 100, t("t_loop"))
    waits = [("s_wr", "t_wr", "t_wr2"), ("s_we", "t_we", "t_we2"), ("s_wu", "t_wu", "t_wu2"),
             ("s_del", "t_del", None)]
    for i, (k, out_k, back_k) in enumerate(waits):
        c = 200 + i * 230
        d.box(c - 100, 320, 200, 44, t(k), small=True)
        d.arrow([(c - 40, 210), (c - 40, 320)], t(out_k), at=(c - 46, 252), anchor="end")
        if back_k:
            d.arrow([(c + 40, 320), (c + 40, 210)], t(back_k), at=(c + 46, 292), anchor="start", acc=True)
    d.arrow([(990, 342), (1040, 342), (1040, 52), (900, 52)], t("t_del2"), at=(1034, 300), anchor="end",
            acc=True)
    figs.append((t("f5"), d.svg(), t("f5c")))

    # ---------- 图 6：续跑策略 ----------
    d = D(1040, 330, t("f6c"))
    d.box(20, 146, 170, 48, t("c_res"), small=True)
    d.box(230, 146, 170, 48, t("c_cls"), acc=True, small=True)
    d.arrow([(190, 170), (230, 170)])
    d.box(450, 40, 160, 40, t("c_ok"), small=True)
    d.box(450, 150, 160, 40, t("c_oom"), small=True)
    d.box(450, 250, 160, 40, t("c_other"), small=True)
    for y in (60, 170, 270):
        d.arrow([(400, 170), (425, 170), (425, y), (450, y)])
    d.box(760, 20, 260, 52, t("c_next"), [t("c_next_s")])
    d.box(760, 144, 260, 52, t("c_retry"), [t("c_retry_s")])
    d.box(760, 248, 260, 60, t("c_llm"), [t("c_llm_s")], acc=True)
    d.arrow([(610, 50), (760, 46)], t("c_more"), at=(685, 40))
    d.arrow([(610, 170), (760, 170)], t("c_left"), at=(685, 163))
    d.arrow([(610, 270), (760, 270)])
    d.arrow([(610, 68), (640, 68), (640, 296), (760, 296)], t("c_last"), at=(646, 120), anchor="start")
    d.arrow([(610, 182), (700, 182), (700, 296)], t("c_none"), at=(706, 236), anchor="start")
    figs.append((t("f6"), d.svg(), t("f6c")))

    # ---------- 图 7：任务状态 ----------
    d = D(1080, 300, t("f7c"))
    chain = ["k_draft", "k_val", "k_wm", "k_sub", "k_run", "k_rev"]
    xs = [20, 175, 330, 485, 640, 795]
    for i, k in enumerate(chain):
        d.box(xs[i], 110, 130, 44, t(k), small=True, acc=(k == "k_rev"))
        if i:
            d.arrow([(xs[i - 1] + 130, 132), (xs[i], 132)])
    d.box(960, 60, 100, 40, t("k_done"), small=True)
    d.box(960, 164, 100, 40, t("k_rej"), small=True)
    d.arrow([(925, 124), (960, 84)]); d.arrow([(925, 142), (960, 180)])
    d.arrow([(1010, 60), (1010, 30), (860, 30), (860, 110)], t("k_reopen"), at=(935, 24), acc=True)
    d.box(250, 230, 130, 40, t("k_fail"), small=True)
    d.box(560, 230, 130, 40, t("k_to"), small=True)
    for x in (85, 240, 395, 550, 705):
        d.raw(f'<line class="ln faint" x1="{x}" y1="154" x2="{x}" y2="196"/>')
    d.raw('<line class="ln faint" x1="85" y1="196" x2="705" y2="196"/>')
    d.arrow([(315, 196), (315, 230)], t("k_err"), at=(323, 220), anchor="start")
    d.arrow([(625, 196), (625, 230)], t("k_gate"), at=(633, 220), anchor="start")
    d.arrow([(690, 250), (860, 250), (860, 154)], t("k_late"), at=(700, 244), anchor="start", acc=True)
    figs.append((t("f7"), d.svg(), t("f7c")))

    th = T["th"][lang]
    rows = "".join(f"<tr><td>{a}</td><td><code>{b}</code></td><td>{c[lang]}</td></tr>" for a, b, c in ROWS)
    body = "".join(f'<h2>{h}</h2><figure>{svg}<figcaption>{cap}</figcaption></figure>'
                   for h, svg, cap in figs)
    return PAGE.format(lang="zh-CN" if lang else "en", title=t("title"), updated=t("updated"),
                       other=t("other"), lead=t("lead"), body=body, tbl=t("tbl"),
                       th0=th[0], th1=th[1], th2=th[2], rows=rows)


PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f6f8fb;--card:#fff;--line:#c9d3e0;--text:#152030;--muted:#526175;--accent:#1f5f99;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#10151e;--card:#1a2331;--line:#344258;
  --text:#e7edf7;--muted:#a9b8cc;--accent:#8ecbff;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#10151e;--card:#1a2331;--line:#344258;--text:#e7edf7;--muted:#a9b8cc;--accent:#8ecbff;color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:15.5px/1.7 system-ui,-apple-system,"PingFang SC",sans-serif}}
main{{max-width:1180px;margin:auto;padding:28px 20px 70px}}
h1{{font-size:clamp(24px,4vw,32px);margin:4px 0 8px}} h2{{font-size:19px;margin:38px 0 10px}}
.muted{{color:var(--muted)}} .small{{font-size:13.5px}}
.lead{{border-left:3px solid var(--accent);background:var(--card);padding:12px 18px;border-radius:0 10px 10px 0;margin:14px 0}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 14px 10px}}
figure svg{{width:100%;height:auto;display:block;color:var(--text)}}
figcaption{{color:var(--muted);font-size:13.5px;margin-top:8px}}
svg text{{fill:currentColor;font-size:12px;font-family:system-ui,-apple-system,"PingFang SC",sans-serif}}
svg .t{{font-weight:650;font-size:12.5px}} svg .ts{{font-weight:600;font-size:11.5px}}
svg .s{{fill:var(--muted);font-size:11px}} svg .lbl{{fill:var(--muted);font-size:10.5px}}
svg .b rect{{fill:var(--card);stroke:currentColor;stroke-opacity:.5}}
svg .b.acc rect{{stroke:var(--accent);stroke-width:1.8;stroke-opacity:1}}
svg .b.dash rect{{stroke-dasharray:5 4}}
svg .ln{{stroke:currentColor;stroke-opacity:.65;stroke-width:1.3}}
svg .ln.dash{{stroke-dasharray:5 4}} svg .ln.accl{{stroke:var(--accent);stroke-opacity:1;stroke-width:1.6}}
svg .ln.faint{{stroke-opacity:.35}} svg .accf{{fill:var(--accent)}}
svg .grp{{fill:none;stroke:currentColor;stroke-opacity:.25;stroke-dasharray:3 4}}
svg .store{{fill:var(--bg);stroke:currentColor;stroke-opacity:.35}}
svg .life{{stroke:currentColor;stroke-opacity:.18;stroke-dasharray:2 4}}
.table{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:7px 10px;border:1px solid var(--line);text-align:left;vertical-align:top}} th{{background:var(--card)}}
code{{color:var(--accent);font-size:.92em}}
</style>
</head>
<body>
<main>
<div class="muted small">{updated} · {other}</div>
<h1>{title}</h1>
<div class="lead">{lead}</div>
{body}
<h2>{tbl}</h2>
<div class="table"><table><tr><th>{th0}</th><th>{th1}</th><th>{th2}</th></tr>{rows}</table></div>
</main>
</body>
</html>
"""

if __name__ == "__main__":
    (ROOT / "docs" / "architecture.html").write_text(build(0))
    (ROOT / "docs" / "architecture.zh-CN.html").write_text(build(1))
    print("wrote docs/architecture.html and docs/architecture.zh-CN.html")
