# aco_lw 运行手册（Runbook）

更新于 2026-11-07 · 适用于 `va daemon` + 控制台 + Telegram + 通用 agent（工具、看图、MCP、在线大脑、权限模式）· English: [runbook.md](runbook.md)

本手册回答三件事：**怎么开 / 关**、**日常怎么用**、**出问题怎么查**。进度见 `docs/progress.zh-CN.html`，架构图与流程图见 `docs/architecture.zh-CN.html`（控制台 →「架构」）。

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
| 规划器规则 | `prompts/planner_rules.md` · `prompts/agent_rules.md` | 可直接编辑；改动会让 LLM 前缀缓存失效一次 |
| 执行策略 | `policies.yaml` | 每个工具的允许 / 审批 / 上限；MCP 审批规则；`run_command.auto_allow` |
| agent 工作区与记忆 | `data/agent-work/<运行ID>/` · `data/agent_memory/` | 命令在工作区里跑；记忆是 agent 自己的笔记（`MEMORY.md` 为索引） |
| 数据 | `data/` | `templates/`（晋升的模板）、`variants/`（导入的 workflow）、`tasks.sqlite`（索引）、`jobs/<任务ID>/`（manifest + 产物）、`packs/<项目ID>/v<N>/`（基准包）、`uploads/<项目ID>/`（参考图）、`daemon.log` |

## 2. 启动与停止

### 一共三个服务

| 服务 | 地址 | 作用 |
|---|---|---|
| ComfyUI（带 FreeVideo 插件） | http://127.0.0.1:8188 | 出图、出视频。FreeVideo 不是单独的程序，它是 ComfyUI 里的插件，ComfyUI 起来它就在 |
| LM Studio | http://127.0.0.1:1234 | 本地 LLM（aco 的大脑） |
| aco（`va daemon`） | http://127.0.0.1:8790 | 编排器 + 控制台 + Telegram |

先开哪个都可以：aco 先起来时会每 30 秒重试连接 ComfyUI，连上之前排队的任务原地等着。

### 1）启动 ComfyUI + FreeVideo
本机没有装 FreeVideo.app，ComfyUI 用 FreeVideo-local 自带的 Python 直接启动（参数与 FreeVideo 启动器一致，不要改）。

前台运行（开一个终端窗口专门放它，`Ctrl-C` 停止）：

```sh
cd ~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI
../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188
```

后台运行（关掉终端也不停）：

```sh
cd ~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI
nohup ../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188 >/dev/null 2>&1 &
```

- 约 10–20 秒后就绪（实测 18 s，含 aco 一起启动）。检查：`curl -s http://127.0.0.1:8188/system_stats` 有 JSON 返回；或浏览器打开 http://127.0.0.1:8188 。
- 打开 FreeVideo 面板：浏览器进 http://127.0.0.1:8188 → **工作流 → 浏览模板 → FreeVideo → FreeVideo-All-in-One**；FreeVideo agent 对话在 **Extensions → FreeVideo**。
- 日志：FreeVideo 插件把 ComfyUI 输出写到 `~/Documents/ChatGPT/jev/FreeVideo-local/launcher/comfy-runs/connected-<时间>-<pid>/comfy.log`（最新的目录就是这次）。
- 停止：前台 `Ctrl-C`；后台 `pkill -f "ComfyUI/main.py"`。停之前先确认没有在跑的视频，否则那条会中断。
- `envs/comfyui-<hash>` 目录名以后 FreeVideo 升级可能变；以 `FreeVideo-local/launcher/comfy-host.json` 里的 `python` 为准。

### 2）启动 LM Studio
打开 LM Studio 应用即可（或终端 `lms server start`）。不需要手动加载模型：aco 用到时自己加载、空闲时卸载。检查：`curl -s http://127.0.0.1:1234/v1/models`。

### 3）启动 aco
前台运行（`Ctrl-C` 停止，日志同时写进 `data/daemon.log`）：

```sh
cd ~/workplace/video-agent
uv run va daemon 2>&1 | tee -a data/daemon.log
```

后台运行：

```sh
cd ~/workplace/video-agent
nohup uv run va daemon --port 8790 >> data/daemon.log 2>&1 &
```

- 就绪标志：日志出现 `Telegram listener started (whitelist [...])` 和 `Console: http://127.0.0.1:8790`。检查：`curl -s http://127.0.0.1:8790/api/system`，返回里 `comfy.online` 为 `true`、`daemon.alive` 为 `true`。
- 打开控制台：浏览器进 http://127.0.0.1:8790 ；Telegram 直接给 bot 发消息。
- 启动时会自动对账（`recover`），只收集已完成的任务，**不会重复提交**。ComfyUI 没开时日志会出现 `Startup reconcile failed (ComfyUI offline?)`，开了 ComfyUI 之后自动继续，不用重启 aco。
- 看日志：`tail -f data/daemon.log`。

### 一键全开（后台）

```sh
FV=~/Documents/ChatGPT/jev/FreeVideo-local
(cd $FV/ComfyUI && nohup ../envs/comfyui-630aa60d8624e901/bin/python main.py --listen 127.0.0.1 --port 8188 >/dev/null 2>&1 &)
(cd ~/workplace/video-agent && nohup uv run va daemon --port 8790 >> data/daemon.log 2>&1 &)
```

### 看哪些在运行

```sh
pgrep -fl "ComfyUI/main.py"; pgrep -fl "va daemon"
curl -s -o /dev/null -w "ComfyUI %{http_code}\n" http://127.0.0.1:8188/system_stats
curl -s -o /dev/null -w "aco %{http_code}\n" http://127.0.0.1:8790/api/system
```

两个都是 `200` 就是正常。

### 停止
- aco：前台 `Ctrl-C`；后台 `pkill -f "va daemon"`。
- 正在 ComfyUI 里跑的任务**不会因为停 aco 而取消**；下次启动 aco 会按 prompt_id 对账并收回产物。
- 重启 aco 前先看控制台没有“运行中”的任务、agent 没在回答。

### 只看不跑
`uv run va serve`：只开控制台（浏览 + 审阅），不出图。排队任务要等 daemon 起来才执行。

## 3. 日常使用

### 3.1 一次完整的创作循环
1. **提需求**：控制台「项目与对话」或 Telegram，中英文都可以。例：“同一个金发学院少女，回头看向镜头的半身照，傍晚暖色窗光”。可以**附参考图**（控制台 📎，或在 Telegram 直接发图）并说“参照这张脸”。
   Telegram 里**只发图、不写说明**时，aco 先回一句“收到”，不开始干活；30 分钟内你的下一条消息会把图带上（例如先发定妆照，再发“用这个定妆生成一个视频”）。agent 问你要图时，直接发图就算回答。
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
| `/round 模板 [数量] [prompt]` | 不经 LLM 直接出一轮（模板见 `va templates`，如 `zimage-t2i` `flux2-t2i` `flux2-edit` `h3-t2v` `h3-i2v`） |
| `/redo` | 最近一轮待审阅的整轮重做 |
| `/reopen [轮ID]` | 更改最近一轮（或指定轮）的审阅结果 |
| `/pause` `/resume` | 暂停 / 恢复出图（正在跑的会跑完） |
| `/cancel` | 取消当前项目的排队任务，并中断本系统正在跑的任务 |
| `/mode [on_demand\|session\|always\|command_only]` | LLM 驻留模式（默认按需） |
| `/project` · `/projects` · `/project new 名称` · `/project use 名称` · `/project done` | 项目管理 |
| `/session` · `/session new [标题]` · `/session use 序号` | 会话（序号 1 = 最近活跃） |
| `/memory` · `/memory add 文本` · `/memory del 编号` | 项目记忆 |
| `/pack` | 当前基准包 |
| `/model [名字]` | 查看 / 切换 agent 的大脑（本地或在线，见 §3.10） |
| `/llm` · `/llm use 名称` · `/llm load` · `/llm unload` | 列出本机已下载的模型；切换 aco 用的本地模型（先卸载旧的、检查内存，加载好了告诉你）。命令行等价于 `va llm …` |
| `/permissions [default\|bypass\|forget 规则\|all]` | 审批模式与“始终允许”规则（见 §3.10） |
| `/fv 任务` | 把视频任务交给 FreeVideo agent |
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
主动切换在线大脑用 `/model`（见 §3.10）；下面是本地跑不了时的**自动回退**。

本地 LLM 跑不了时（最常见是视频渲染占着内存），请求默认会等待。也可以在 `config.toml` 里让它改在线上继续：

```toml
allow_online = true                                   # 隐私开关，默认关
llm_fallback = ["anthropic", "claude_code", "codex"]  # 依次尝试
```

- `anthropic`、`openrouter`（或任意 `openai_compatible` 接口）：在线 API 接着同一次请求逐步走。key：`ANTHROPIC_API_KEY` / `ant auth login` 或 `anthropic_api_key`；`OPENROUTER_API_KEY` 或 `openrouter_api_key`（另需 `openrouter_model`）。
- `claude_code` / `codex`：本机 CLI 用它自己的登录和在线模型跑完整个请求。经 MCP 它**只拿到 aco_lw 的工具**（Claude Code 关掉全部内置工具，Codex 在只读沙箱里）；每次工具调用仍走执行策略、审批与调度。
- 不设 `allow_online = true` 就不会往外发任何内容，控制台会显示请求在等资源及原因。
- 对话气泡上会标注由哪个后端回答（“经 …”），Claude Code 还会显示花费。

### 3.8 制片：从短篇小说到成片（阶段 3，预览）
制片按**工作流**逐阶段推进；每个阶段都等你通过后才开始下一阶段。第一套工作流是 `novel-to-film`：

| 阶段 | 由谁完成 | 你审核 |
|---|---|---|
| 改编（一句话梗概、场次、节拍） | LLM + skill `adapt-novel` | 通过 / 带意见重做 / 手改 JSON |
| 设定圣经（风格、人物、场景） | LLM + skill `story-bible` | 同上 |
| 人物定妆、场景基准图 | FLUX.2，每个人物 / 场景一轮 | 在「审阅」里选中候选 |
| 分镜（景别、运镜、动作、每镜参考） | LLM + skill `storyboard` | 通过 / 重做 / 手改 |
| 关键帧（以已通过的定妆图为参考编辑） | FLUX.2 编辑，每镜一轮 | 在「审阅」里选中 |
| 镜头视频 | H3 图生视频，每镜一条——**你点「开始」才跑** | 选中 / 重做 |
| 剪辑（顺序、裁切、转场、字幕） | LLM + skill `edit-timeline` | 通过 / 重做 / 手改 |
| 成片 | FFmpeg 按时间线合成（保留镜头自带的声音——H3 会生成人声；软字幕 + `.srt`） | 通过 |

- **输入**：一段故事**或**对想要的片子的描述，另可加**链接**（网页：正文 + 主图；YouTube：标题、简介、章节、字幕、缩略图，以及按 `youtube_frames` 抽的几帧）和**参考图**。每张图填名字、类型（人物 / 场景 / 风格 / 道具）和用法：「按片子风格重画」（FLUX 以你的照片为参考编辑出定妆图）或「原样使用」（你的照片直接作为已通过的定妆图）。所有来源先整理成一份**素材包**；某个链接失败只标出原因，其余照常。
- 任何渲染条目（定妆图、场景图、关键帧，甚至镜头视频）都可以点 **「用我的图片…」** 换成你的文件，视为已通过；依赖它的下游会重出。
- **控制台 → 制片 → 新建**：贴原文，选视频档位（默认 `smoke`，已实测）和图片候选数。规划阶段马上开始（本地 LLM；视频渲染期间排队，除非设了 `allow_online` + `llm_fallback`）。
- **改上游只影响依赖它的下游**：改一个人物，只重出该人物的定妆图和用到它的镜头；其他镜头保持已通过。输入变了的规划阶段会显示“上游变了——保留还是重做？”。
- **对白语言**：新建制片时填写（或之后在「设置」里改）。分镜和剪辑会用它写画面、动作和台词，每条视频提示词也会要求用它说话——H3 按提示词的语言生成人声。
- **影响很多镜头的修改**（“所有镜头用普通话”“整体调暗”）：打开「分镜」阶段，点「重做…」写上这条意见。新分镜通过前，已通过的旧分镜继续有效；通过后只有文字变了的镜头会重出（关键帧自动，视频等你点开始）。在单条视频轮上打回写的意见只影响那一轮。
- 规划阶段待审核时 Telegram 会收到消息；渲染轮照常带按钮推送。
- 对话里的 agent（以及经 MCP 的 Claude Code / Codex）可用 `create_production`、`get_production_plan`、`run_stage`、`review_stage` 驱动制片；视频阶段仍需你点头。
- 扩展：工作流是 `workflows/`（或 `data/workflows/`）下的 YAML，输出契约是 `workflows/schemas/` 里的 JSON Schema，skill 是 Claude Code 格式的 `SKILL.md` 目录，放在 `skills/`（你自己的放 `data/skills/`）。新类型的片子 = 新 YAML + skill，不改代码。

### 3.9 用 FreeVideo 电影保持人物一致；agent 的 ComfyUI 工具
- **工作流 `story-to-film`**：人物变成 **FreeVideo 角色卡**（最多 6 张标注图 + 声音；卡库里有同名卡就复用，否则用已通过的定妆图新建）。镜头由 **一部 FreeVideo 电影** 拍：每镜按 `video_candidates` 拍几条 take，以角色卡为参考；标了 *continue* 的镜头从你选中那条的末帧续拍。take 拍完以一轮候选出现在 **审阅** 里；选中会告诉 FreeVideo；*重做 / 带意见打回* 变成 FreeVideo 按你的意见重拍。续拍镜头至少 10 秒、质量只能 light / medium。暂停时选“立即停止”会取消没拍完的 take。
- 提示词遵循 MiniMax 官方 **h3-prompt-writing** skill（装在 `data/skills/`）；FreeVideo 把每个镜头编成 H3 参考格式。
- **ComfyUI 工具（artokun/comfyui-mcp，由我们的 daemon 启动）**：agent 可以查模型和节点、读与搭建 workflow、读模型族指南（`comfy_catalog`、`comfy`）。运行 workflow 走我们的队列（`run_comfy_workflow`，可追踪，受暂停与内存闸门）。装节点、下模型、训练要你批准（按钮）；重启 ComfyUI、删模型、动队列里别人的任务、释放显存、改 ComfyUI 设置、云端 / 付费节点一律拒绝。
- **FreeVideo 的批准**：FreeVideo 可以要求开拍电影、长时间重拍前由你在它的页面上批准（ComfyUI → Extensions → FreeVideo → agent 对话）。设置是文件 `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI/output/FreeVideo/.agent/confirm.json`（这个 FreeVideo 版本没有页面开关；默认 `{"start": true, "reshoot_seconds": 300}`）。**2026-10-27 按用户要求设为 `{"start": false, "reshoot_seconds": null}`**：开拍与重拍在 FreeVideo 里不再询问；我们自己的「开始」按钮和审核照常。要恢复：把 `start` 改回 `true`，和 / 或把 `reshoot_seconds` 设成秒数。FreeVideo 每次检查都会重读这个文件。

### 3.10 通用 agent：工具、看图、在线大脑、权限（2026-11-07）
自由文本交给 agent。LLM 只**提出** tool call，编排器执行并择机把结果交回（LLM 被卸载时结果暂存）。它自己选工具和 skill：

| 工具 | 作用 | 审批 |
|---|---|---|
| 生成工具（`queue_round`、`wait_for_round`、制片、ComfyUI 探索…） | 出图出视频、审阅、制片 | 未实测的视频档位：估算耗时超过 20 分钟（`policies.yaml` 的 `budget_min`）或估不出来时才要批准；你在消息里点名了档位就不问 |
| `web_fetch` | 读网页正文（无登录、无 JS） | 不用 |
| `run_command` | 在本次运行的工作目录 `data/agent-work/<运行ID>/` 里跑 zsh 命令（如 agent-reach、ffmpeg） | **要**（见下面的权限） |
| `read_file` / `write_file` | 读 `data/` 与 skill 目录；只写工作目录 | 不用 |
| `list_skills` / `load_skill` | `data/skills/` 与 `~/.claude/skills`（`agent_skill_dirs`） | 不用 |
| `memory_read` / `memory_write` | agent 自己的记忆 `data/agent_memory/`，索引进系统提示词 | 不用 |
| `look` | **看图**：图片缩到 1024 交给视觉 LLM；视频抽 6 帧；`@round:ID` 看整轮 | 不用 |
| `add_media` / `send_file` | 把文件变成 `@upload:ID` / 发给你（路径也可写 `@task:ID`） | 不用 |
| `mcp_list` / `mcp_call` | `config.toml` 的 `[mcp_servers.*]`（格式同 Claude Code；当前 Exa） | 只读工具不用，其余要 |
| `delegate_agent` | 把子任务交给 Claude Code / Codex，等它汇报 | 要；需 `allow_online` |
| `delegate_freevideo`（`/fv`） | 整个视频任务交给 FreeVideo agent | 要；需 `freevideo_agent_online` |

**硬拒绝（任何模式都拒绝）**：直连 ComfyUI `:8188`、读 FreeVideo token、`kill` / `pkill` 结束进程。加载 / 切换 LLM（`va llm use`、`lms load`）是普通的需审批命令。命令可带 `memory_gib` / `exclusive` 资源预估：批准后编排器等内存够了才执行（会等视频渲染结束；独占时先卸载 LLM），审批消息里显示预估。执行策略在 `policies.yaml`（每个工具的开关、参数上限、MCP 审批规则）。

**权限模式**（类似 Claude Code 的 permission mode）：
- `default`（默认）：要审批的动作逐条发按钮：**✅ 同意 / ✅ 始终允许（规则）/ ✕ 不要**。“始终允许”记一条规则，以后同类不再问：命令按程序名（`cmd:ffmpeg`，只对不带管道 / 串联 / 重定向的单条命令）、MCP 按工具（`mcp:exa.web_search_exa`）、委托按对象（`delegate:codex`）、未实测视频档位按模板（`round:h3-t2v`）。
- `bypass`：**不再审批**，直接执行（相当于 `bypassPermissions`）。每次自动执行仍会发一行“⚡ 已自动批准”留痕；硬拒绝、执行策略、在线开关照旧；**装 ComfyUI 节点 / 下载模型仍然每次问你**（硬约束）。`/status` 会显示 ⚡。
- 切换：`/permissions bypass` · `/permissions default` · `/permissions` 查看模式与规则 · `/permissions forget <规则|all>` 删除规则。启动默认值：`config.toml` 的 `permission_mode`（`/permissions` 存库，优先）。
- 另有静态放行：`policies.yaml` 的 `run_command.auto_allow`（命令前缀列表）。

**在线大脑**（需 `allow_online = true`；关掉时自动退回本地，不往外发）：`/model` 查看，`/model <名字>` 切换：

| 名字 | 是什么 | 前提 |
|---|---|---|
| `local` | 本地 27B（LM Studio，视觉模型） | 无 |
| `openai` | OpenAI 官方 API（ChatGPT 的模型） | `openai_model` + `OPENAI_API_KEY` / `openai_api_key` |
| `openrouter` | OpenRouter 上任意模型 | `openrouter_model` + `OPENROUTER_API_KEY` / `openrouter_api_key` |
| `anthropic` | Claude API | `ANTHROPIC_API_KEY` / `ant auth login` |
| `openai_compatible` | 任意 OpenAI 兼容服务 | `openai_base_url` + `openai_model` |
| `codex` | 整个请求交给 Codex CLI | 本机 Codex 登录（可用 **ChatGPT 账号**，不需要 API key） |
| `claude_code` | 整个请求交给 Claude Code | 本机 Claude Code 登录 |

ChatGPT 订阅本身不是 API：用订阅走 `codex`；`openai` 走按量计费的 API key。Codex / Claude Code 只拿到 aco_lw 的工具（经 MCP），命令照样受权限模式约束。

### 3.11 人物 LoRA：锁住同一个人
- **装到哪**：LoRA 文件放进 `FreeVideo-local/ComfyUI/models/loras/`。aco 自动读取文件里记录的底模，列进 agent 的提示词，并写明哪些模板能用它。
- **写说明（建议）**：在 `data/loras.yaml`（不进 git）给每个 LoRA 写上人物、风格、推荐强度和触发词，例如：
  ```yaml
  my-face.safetensors:
    person: 女主
    style: photo
    strength: 0.5
    trigger: null
  ```
  改完不用重启，下一次对话就生效。
- **模板**：`flux2-lora-t2i`（文生图）和 `flux2-lora-edit`（参考图 + LoRA）。参数 `lora` 必填，`lora_strength` 一般 0.45–0.6。
- **视频怎么锁人**：
  - LoRA 是 Klein 4B 的图片 LoRA，不能直接加载到 H3 视频模型里，要先用它出人物图，再交给视频。
  - agent 会先加载 `character-consistency` skill：用 LoRA 出正脸和每个镜头的首帧，再用 `freevideo-ref` 出视频（不带参考视频，只给两张参考图，脸会贯穿整段）。
  - 长视频拆成 5–10 秒的镜头，每个镜头用自己的首帧，最后拼接。
  - `h3n-r2v` 在 Mac 上跑不了（MPS 缺 int8 矩阵乘法）。
- **跟你确认**：agent 会用 `propose_memory` 提议「某人物 → 某 LoRA、强度」，你确认后，这个项目以后都会自动用上。

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
| “ComfyUI 离线” | 打开 http://127.0.0.1:8188 | 按 §2「启动 ComfyUI + FreeVideo」启动；aco 不会自己启动它，连上后自动继续 |
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
- 只有编排器能碰 ComfyUI；LLM 只提出 tool call，由编排器执行。命令默认逐条经你批准（或“始终允许”规则 / 你打开的 bypass 模式）；硬拒绝在任何模式下都生效。
- 不取消、不释放别人的任务；`/free` 前二次确认队列为空。
- 不修改 ComfyUI 启动参数、全局 LM Studio / OpenClaw 配置；下载模型、装自定义节点须经你批准。
- 在线：不设 `allow_online` 就不往外发任何对话（`/fv` 只看 `freevideo_agent_online`）。
- Telegram 只响应白名单；控制台只绑定 127.0.0.1。
- LLM 提议的项目记忆需要你确认才生效。

## 8. 已知问题（2026-10-26）
1. 规划器偶尔误解有歧义的词（例如“太小”指年龄还是画面）。批注写得越具体越稳；「查看 LLM 上下文」能看到它收到了什么。
2. 规划回复要 10–20 s，主要是模型“思考”，该模型无法通过 API 关闭（`/no_think` 可减半，见 `planner_fast`）。
