# aco_lw 运行手册（Runbook）

更新于 2026-10-26 · 适用于阶段 2.5 + W2（`va daemon` + 控制台 + Telegram + 会话与项目记忆）· English: [runbook.md](runbook.md)

本手册回答三件事：**怎么开 / 关**、**日常怎么用**、**出问题怎么查**。设计取舍见 `docs/design.html`，计划与进度见 `PLAN.md` / `docs/progress.html`，实测数据见 `docs/baseline.md`。

---

## 1. 组件与位置

| 组件 | 地址 / 位置 | 说明 |
|---|---|---|
| aco_lw 控制台 | http://127.0.0.1:8790 | `va daemon` 启动；只绑定本机。**8765 是另一个 server，不要占用** |
| ComfyUI（FreeVideo-local） | http://127.0.0.1:8188 · `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` | 执行端：FLUX.2 出图、FreeVideo / MiniMax H3 出视频。aco_lw 不负责启动它 |
| LM Studio | http://127.0.0.1:1234 · CLI `lms` | 27B 规划器（`qwen3.8-27b-splash`），按需加载、空闲卸载 |
| Telegram bot | @luwu_abot | 白名单 chat_id 见 `config.toml` |
| 代码 | `~/workplace/video-agent`（GitHub 私有仓库 `aco_lw`） | |
| 配置 | `config.toml`（gitignore，权限 600） | token 等敏感项只在这里 |
| 规划器规则 | `prompts/planner_rules.md` | 可直接编辑；改动会让 LLM 前缀缓存失效一次 |
| 数据 | `data/` | `templates/`（晋升的模板）、`variants/`（导入的 workflow）、`tasks.sqlite`（索引）、`jobs/<任务ID>/`（manifest + 产物）、`packs/<项目ID>/v<N>/`（基准包）、`uploads/<项目ID>/`（参考图）、`daemon.log` |

## 2. 启动与停止

### 启动顺序
1. **ComfyUI**：确认 http://127.0.0.1:8188 能打开（FreeVideo-local 环境）。
2. **LM Studio**：应用开着即可；不需要手动加载模型。
3. **aco_lw**：执行下面的命令。

```sh
cd ~/workplace/video-agent
uv run va daemon 2>&1 | tee -a data/daemon.log
```

看到 `Telegram listener started (whitelist [...])` 与 `Console: http://127.0.0.1:8790` 即就绪。启动时会自动对账（`recover`），只收集已完成的任务，**不会重复提交**。

### 停止
- 在运行 daemon 的终端按 `Ctrl-C`，或 `pkill -f "va daemon"`。
- 正在 ComfyUI 里跑的任务**不会被取消**；下次启动 daemon 会按 prompt_id 对账并收回产物。

### 只看不跑
`uv run va serve`：只开控制台（浏览 + 审阅），不出图。排队任务要等 daemon 起来才执行。

## 3. 日常使用

### 3.1 一次完整的创作循环
1. **提需求**：控制台「项目与对话」或 Telegram，中英文都可以。例：“同一个金发学院少女，回头看向镜头的半身照，傍晚暖色窗光”。可以**附参考图**（控制台 📎，或在 Telegram 直接发图）并说“参照这张脸”。
   规划器先秒回“收到”，通常 10–20 s 理解（冷启动多约 15 s 加载）后排队一轮候选（图片默认 4 张，视频 1 条）。
2. **等出图**：图片约 12 s / 张；视频 640×384×3s 约 5–6 min。完成后控制台「审阅」出现角标，Telegram 收到原图文件 + 按钮。
3. **审阅**（控制台 `#review` 或 Telegram 按钮，二者等价）：

   | 动作 | 效果 |
   |---|---|
   | ✅ 通过 | 选中该候选，其余标为打回；可同时**锁进基准包**（角色 / 场景 / 风格） |
   | 🎲 整轮重做 | 整轮不要，同参数换种子再出一轮 |
   | 🔧 微调 | 写一句修改说明；保留 #N 的种子，LLM 改写 prompt 后出新一轮 |
   | ↩️ 打回+批注 | 写原因；LLM 按批注调整参数后出新一轮（留空 = 只打回） |
   | ✏️ 手动微调 | 自己在表单里改参数（prompt、seed、steps、参考图、档位、数量），不经 LLM |
   | ↺ 更改审阅结果 | 把已决定的轮重新打开；旧决定进历史，它锁进基准包的资产会在新版本中撤销 |

   也可以直接打字：“2 号不错，锁成角色”“年龄太小了”。有待审阅的轮次时，对话里的反馈会作用在那一轮上，新一轮在 DAG 上连到它。
   控制台快捷键：`1–9` 高亮 · `Enter` 通过 · `F` 微调 · `R` 重做 · `X` 打回 · `J/K` 切换审阅项。
4. **锁定后延续**：基准包锁定“角色”后，后续说“用这个角色……”，规划器会用 `@pack:角色` 作为参考图（参考编辑 / 图生视频）。

### 3.2 项目、会话与项目记忆
- **项目** = 一个创作目标。轮次、任务、基准包、会话、记忆都挂在项目下。已完成的项目永久可浏览（「项目与对话」筛选“已完成”）。
- **会话** = 项目里的一条对话线。换话题时新开（`+ 新会话` 或 `/session new`），旧会话随时可以接着聊。会话功能上线前的旧消息在“历史会话”里。
- **项目记忆** = 本项目所有会话共享的长期设定（如“镜头保持 35mm”“约 19 岁，精致 CG 偏真人”）。LLM 在相关对话后**提议**，你点**记住**才生效（控制台「项目记忆」页或 Telegram 按钮）。可以添加、编辑（生成新版本）、置顶、归档、恢复。
- **长会话**自动摘要：较早的往来压缩进会话摘要，原消息仍保留在历史里。
- **查看 LLM 上下文**（控制台，按会话）：显示规划器下一次将收到的完整内容，理解出错时先看这里。

### 3.3 命令（Telegram 与控制台对话框通用，不唤醒 LLM）

| 命令 | 作用 |
|---|---|
| `/status` | 项目、出图状态、排队数、内存、LLM 模式 |
| `/review` | 把当前项目待审阅的轮次重新推送到 Telegram |
| `/round 模板 [数量] [prompt]` | 不经 LLM 直接出一轮（模板：`flux2-t2i` `flux2-edit` `h3-t2v` `h3-i2v`） |
| `/redo` | 最近一轮待审阅的整轮重做 |
| `/reopen [轮ID]` | 更改最近一轮（或指定轮）的审阅结果 |
| `/pause` `/resume` | 暂停 / 恢复出图（正在跑的会跑完） |
| `/cancel` | 取消当前项目的排队任务，并中断本系统正在跑的任务 |
| `/mode [on_demand\|session\|always\|command_only]` | LLM 驻留模式（默认按需） |
| `/project` · `/projects` · `/project new 名称` · `/project use 名称` · `/project done` | 项目管理 |
| `/session` · `/session new [标题]` · `/session use 序号` | 会话（序号 1 = 最近活跃） |
| `/memory` · `/memory add 文本` · `/memory del 编号` | 项目记忆 |
| `/pack` | 当前基准包 |
| `/whoami` | 显示 chat_id（任何人可用，用于配置白名单） |

### 3.4 命令行（本机）

```sh
uv run va templates                    # 模板、档位（✓ 已实测）、参数
uv run va round flux2-t2i -n 4 -p prompt="..."   # 排队一轮（daemon 执行）
uv run va status [任务ID]               # 任务列表 / 单个 manifest
uv run va project list|new|use|done     # 项目
uv run va pack                          # 基准包版本
uv run va mode [模式]                   # LLM 驻留模式
uv run va recover                       # 手动对账（daemon 启动时会自动做）
uv run va release                       # 队列空闲时释放 ComfyUI 模型
```

### 3.5 语言
控制台默认**英文**，顶栏 EN / 中文 切换（记在浏览器里）。系统消息与 Telegram 文字跟随 `config.toml` 的 `language`（默认 `en`，中文填 `zh`）。规划器用你说话的语言回复。

### 3.6 在 ComfyUI 里改 workflow（高级）
参数表达不了的改动——加节点、改连线、换模型、加 LoRA：
1. 打开一个任务（任务流 → 节点 → 候选），点 **⬇ 导出 workflow**，得到这个任务实际运行的 API workflow。
2. 点 **打开 ComfyUI ↗**，把文件拖到画布上（或 Ctrl+O）。节点 ID 会保留。
3. 改完后在 ComfyUI 里选 **Workflow → Export (API)**。
4. 回到同一个任务，点 **⬆ 导入改好的 workflow** 选这个文件。aco_lw 只显示*你的*改动（相对你导出的版本），列出哪些模板槽位仍能对应，并用 ComfyUI 的 `/object_info` 校验每个节点、取值和连线。有问题则标为*无效*。
5. 在「项目与对话 → Workflow」里：**▶ 试跑** 用你改过的图里的取值、按最低档出一张（只有尺寸取自档位）。
6. 试跑通过后 **⭐ 晋升**，起一个名字（小写字母、数字、连字符）。它成为所有项目可用的正式模板，存在 `data/templates/`；只有试跑过的档位标为已实测。**✕ 否决** 则丢弃并取消它排队中的任务。

候选变体不会被规划器自动选用；晋升前只能手动使用（`/round <变体名>`、手动微调）。

### 3.7 在线模型与本机 agent（回退）
本地 LLM 跑不了时（最常见是视频渲染占着内存），请求默认会等待。也可以在 `config.toml` 里让它改在线上继续：

```toml
allow_online = true                                   # 隐私开关，默认关
llm_fallback = ["anthropic", "claude_code", "codex"]  # 依次尝试
```

- `anthropic`（或 `openai_compatible`）：在线 API 接着同一次请求逐步走。key 来自 `ANTHROPIC_API_KEY` / `ant auth login`，或 `anthropic_api_key`。
- `claude_code` / `codex`：本机 CLI 用它自己的登录和在线模型跑完整个请求。经 MCP 它**只拿到 aco_lw 的工具**（Claude Code 关掉全部内置工具，Codex 在只读沙箱里）；每次工具调用仍走执行策略、审批与调度。
- 不设 `allow_online = true` 就不会往外发任何内容，控制台会显示请求在等资源及原因。
- 对话气泡上会标注由哪个后端回答（“经 …”），Claude Code 还会显示花费。

## 4. 资源与内存策略（自动执行）

| 情况 | 系统行为 |
|---|---|
| 提交视频前 | **卸载 27B**（视频峰值约 55 GiB，共存会进内存压力警告），需要可用 ≥ 62 GiB |
| 视频结束 | 队列空闲则 `/free`；重新加载 27B 并**预热**当前会话的提示缓存 |
| 图片任务 | 不卸载 27B；保留图片模型（热态 11 s vs 冷启动 23 s） |
| 内存不足 | 依次：`/free`（仅队列空闲）→ 卸载 LLM → 等待最多 10 min → 超时不提交 |
| ComfyUI 有别人的任务 | 等队列空闲再提交，不抢占、不取消 |
| 视频生成中收到自由文本 | LLM 排队，生成结束后处理；命令照常即时响应 |
| LLM 空闲 | 会话活跃时 20 min 后卸载（保住提示缓存），其余按 TTL |

**提示缓存（prompt caching）**：LM Studio 只有在新请求是旧请求的“延长”时才复用 KV 缓存（实测 17k token：0.3–0.5 s vs 冷算约 48 s）。因此规划器逐字复现会话里之前的往来，易变状态放在最后；多轮复用通常 55–75%。修改项目记忆、规则文件或摘要重写会整段重算一次。

## 5. 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| 顶栏“daemon 未运行” | `pgrep -fl "va daemon"` | 按 §2 启动；排队任务会自动继续 |
| “ComfyUI 离线” | 打开 http://127.0.0.1:8188 | 启动 FreeVideo-local 的 ComfyUI；aco_lw 不会自己启动它 |
| 任务卡在“等待内存” | 顶栏内存 / 压力；ComfyUI 队列 | 关掉占内存的应用，或 `/cancel`；超过 10 min 会标为超时、不提交 |
| 任务“超时” | `va status <ID>` 看 prompt_id | 生成可能仍在跑：`va recover` 对账收回；不要手动重提 |
| 任务“失败” | 控制台详情页的错误 / ComfyUI 终端日志 | 改参数后“重做”；失败的任务允许同参数重提 |
| Telegram 没反应 | `grep "Ignoring message from non-whitelisted" data/daemon.log` | 发 `/whoami`，把数字加入 `config.toml` 的 `telegram_chat_ids` 后重启 |
| Telegram 发送失败 | `grep "Telegram send" data/daemon.log` | 网络或 token 问题；token 泄露时在 @BotFather `/revoke`，更新 `config.toml` 后重启 |
| 规划器慢 | LLM 气泡上的耗时、token 数、缓存复用 | 正常 10–20 s，主要是模型“思考”。`config.toml` 设 `planner_fast = true` 可省约 35%，理解会略差 |
| 规划器理解错 | 「查看 LLM 上下文」；项目记忆 | 用轮次按钮（微调 / 打回+批注）精确反馈；修正项目记忆；或 `/round` 直接给 prompt |
| 另一个编排器在运行 | 报错 `Another orchestrator…` | daemon 运行时不要同时用 `va submit`；改用 `va round` 排队 |

## 6. 数据、备份与清理
- **备份**：`data/tasks.sqlite`、`data/jobs/`、`data/packs/`、`data/uploads/`、`config.toml`。daemon 停止时复制最安全（SQLite 用 WAL，运行中复制需连同 `-wal` 文件）。
- **产物路径**：控制台任一产物另存；或 `data/jobs/<任务ID>/`。ComfyUI 侧原始文件在 `FreeVideo-local/ComfyUI/output/` 下（`aco_lw/<任务ID>` 前缀或 `FreeVideo/<日期>/`）。
- **清理**：先停 daemon，再删不需要的 `data/jobs/<ID>` 目录（数据库记录仍在，控制台会显示缺图）。

## 7. 安全约束（不要改）
- 只有编排器能碰 ComfyUI；LLM 与 Telegram 只产生数据或调用固定命令。
- 不取消、不释放别人的任务；`/free` 前二次确认队列为空。
- 不修改 ComfyUI 启动参数、全局 LM Studio / OpenClaw 配置；不下载模型、不装自定义节点。
- Telegram 只响应白名单；控制台只绑定 127.0.0.1。
- LLM 提议的项目记忆需要你确认才生效。

## 8. 已知问题（2026-10-26）
1. 规划器偶尔误解有歧义的词（例如“太小”指年龄还是画面）。批注写得越具体越稳；「查看 LLM 上下文」能看到它收到了什么。
2. 规划回复要 10–20 s，主要是模型“思考”，该模型无法通过 API 关闭（`/no_think` 可减半，见 `planner_fast`）。
