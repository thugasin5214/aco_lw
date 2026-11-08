# 轻量本地视频 Agent · 实施 Plan

设计依据：`docs/design.html`（设计稿 · 评审后修订 2026-10-26）。本文件把设计稿落成可执行的范围、阶段和任务清单；设计上的取舍以设计稿为准，这里只写“做什么、按什么顺序、怎样算完成”。

## 1. 一句话目标

一个**确定性 Python 编排器**掌握所有副作用（提交 / 轮询 / 释放 / 重试），驱动本机 ComfyUI 出图出视频；LLM（LM Studio 27B）只是可选规划器、按需唤醒；Telegram 做通知与选版；OpenClaw 最后作为子 agent 接入。

## 2. Scope

### 本轮范围（In scope：阶段 0 + 阶段 1，交付到 G1）

| 模块 | 内容 |
|---|---|
| 基线实测（阶段 0） | LM Studio 模型加载/卸载耗时、ComfyUI `/free` 释放确认、图片/视频模型切换耗时；**640×384、3 秒视频冒烟一次**；记录峰值内存。产出 `data/baseline.json` + `docs/baseline.md` |
| ComfyUI 客户端 | `/prompt`、`/queue`、`/history`、`/free`、`/object_info`、`/system_stats`、`/upload/image` 的薄封装 |
| 模板注册表 | 从现有 API JSON（01 文生图、03 图生视频、04 文生视频、Z-Image）导入；每个模板附一份 slot 映射（参数名 → 节点 ID.字段），带范围约束 |
| 参数档位 | 图片 `1280×720`；视频 `640×384×3s` 起步，逐级上调；生产任务只能选档 |
| Schema 校验 | pydantic 校验参数范围与未知字段；按 `/object_info` 校验节点类型 |
| 内存门控 | 提交前看可用内存 + 模型注册表里的实测峰值，不够则等待或拒绝 |
| 任务状态机 | 草稿 → 已校验 → 等待内存 → 已提交 → 生成中 → 待审阅 → 完成 / 打回 / 失败 / 超时 |
| 持久化 | 每任务一个目录 + `manifest.json`（参数、种子、模板版本、耗时、峰值内存、prompt_id）；SQLite 做索引 |
| 恢复 | 进程重启后按 `prompt_id` 去 `/history` 对账，**不重复提交** |
| 释放策略 | 任务结束且队列空闲才 `/free`，释放前二次确认队列；`--keep-loaded` 保留同模型 |
| 资源锁 | 单机串行锁（文件锁），与现有 `media_bridge.py` 语义一致 |
| CLI | `va plan`（占位，阶段 2 接 LLM）、`va submit`、`va status`、`va release`，外加 `va run <taskfile>` 批量入口 |
| **Web 进度台（只读，2026-10-26 加入）** | `va serve`：系统状态（可用内存、压力、队列、LLM 是否加载）；任务 DAG（按父子关系，例如定妆图 → 图生视频）；单任务阶段流水线（校验 → 门控 → 提交 → 生成 → 收集 → 待审阅）带耗时；产物画廊（图片 / 视频直接预览）；项目进度页 `docs/progress.html` |

### 明确不做（Out of scope，本轮）

- Telegram、审阅**操作**（网页选版 / 打回）、导演基准包 UI（阶段 2；本轮 Web 只读）
- LLM 规划 / 自由文本理解 / 驻留模式切换（阶段 2）
- 一致性评分（人脸、风格嵌入）、探索预算、L3–L5 探索（阶段 3）
- OpenClaw 子 agent、搜索、Skills、MCP（阶段 4）
- 模型选型与量化、节点级调参、OpenClaw SQLite 警告修复（设计稿 §1 已排除）
- 修改 ComfyUI 启动参数、全局 OpenClaw/LM Studio 配置、下载模型、安装自定义节点

### 硬约束（贯穿所有阶段）

1. LLM 和 Telegram **永远不直接**碰 ComfyUI API 或 shell：agent 只提出 tool call，由编排器执行。（2026-11-07 修订：编排器新增通用工具——读网页、执行命令（每条经用户批准）、读写工作目录、skill、agent 记忆；生成副作用仍只走生成工具。见 `src/video_agent/toolbox.py`。）
2. 不取消、不释放非本系统提交/加载的任务和模型；队列非空拒绝 `/free`。
3. 每个数字标注 **实测 / 估算**；估算项进入验证清单。
4. 产物、数据库、日志全部在本项目 `data/` 下，删目录即卸载。

## 3. 现有资产（复用，不改动）

> 2026-10-26 核实：:8188 上运行的是 **FreeVideo-local** 的 ComfyUI，不是 `~/workplace/localvideo`。

| 资产 | 位置 | 用途 |
|---|---|---|
| ComfyUI 0.34（:8188，PID 由端口查） | `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` | 执行端；自定义节点 FreeVideo、ComfyUI-GGUF、PuLID |
| 视频引擎 | FreeVideo（VDN 驱动的 **MiniMax H3**，节点 `FreeVideoGenerate`），权重 `FreeVideo-local/models/{vdn,encoder}` ≈ 28 GB | 视频冒烟对象。按**当前可用统一内存**自适应决定常驻层数，其余层从磁盘流式加载 → 内存紧时表现为**变慢**而非 OOM |
| 图片模型 | FLUX.2 Klein 4B Q8 GGUF（01/02）；`z_image_bf16`（07 Z-Image） | 图片闭环 |
| API 模板 | `~/Documents/ChatGPT/jev/workflows/标准实验室/0{1,2,3,4}-*-API.json` | 导入模板库的来源 |
| 旧桥接 | `.../openclaw-local-media/media_bridge.py`、`标准实验室/api-tools/comfy_job.py` | 行为参考（串行锁、不重复提交、空闲释放） |
| LM Studio（:1234, `lms`） | `qwen3.8-27b-splash`（17.38 GB，常驻中） | 阶段 0 测加载耗时；阶段 2 规划器 |
| OpenClaw | `~/.local/bin/openclaw` | 阶段 4 |
| 旧环境（不用） | `~/workplace/localvideo`（Sulphur 2 / LTX-2.3） | 仅作参考 |

**模型无关**：视频模型会换（MiniMax H3 更新版、LTX 2.5 等），所以模板注册表 + 模型注册表必须可插拔：新模型 = 新模板目录 + slots.yaml + G0 式实测一条记录，编排器代码不改。

## 4. 阶段与闸门

| 阶段 | 内容 | 闸门（退出条件） | 本轮 |
|---|---|---|---|
| **0 接口与基线实测** | 见 §5 | **G0**：视频冒烟通过；峰值内存、切换耗时已记录 | ✅ **已通过 2026-10-26**，见 `docs/baseline.md` |
| **1 调度器 + 图片闭环 + Web 进度台** | 见 §6 | **G1**：CLI 跑通图片闭环；中断重启后无重复提交；Web 上能看到任务 DAG 与产物 | ✅ **已通过 2026-10-26**（出图 21 s；kill -9 后 recover 只对账，ComfyUI 中仅 1 次提交） |
| 2 Telegram + 审阅 + 项目历史 | 通知与按钮选版、控制台审阅、导演基准包、LLM 规划（schema 校验）、项目与对话历史 | G2：手机上能完成选版；等待期间无 LLM 推理 | **进行中**：代码完成，等 Telegram token 做 G2 实机验收 |
| **3 制片流水线（改版，2026-10-27）** | 小说 → 改编 → 设定与定妆 → 分镜 → 关键帧 → 镜头视频 → 剪辑 → 成片，每段审核通过才进下一段；重点是**基础架构与扩展性**：声明式工作流、带版本与过期传播的产物库、权威计划、Skill 执行端、FFmpeg 执行端、按资源族批量调度（见 §6c-6） | G3：一段 ≤ 1 分钟短篇（6–10 镜，smoke 档）从文字跑到成片；改一个角色只让下游相关产物过期；重启不丢进度；换一套工作流 YAML 不改代码 | **规划中**（等用户确认 §6c-6 的待决项） |
| 4 扩展能力 | ~~OpenClaw 子 agent~~ → 编排器自带通用工具 + MCP（§6c-7） | G4：按需启动、能退出、无孤儿进程 | 已完成（2026-11-07） |
| **2.8 Agent 运行时 + Executor + 资源调度** | LLM tool call 由编排器执行并择机交回；Executor 接口；动态资源调度（见 §6c-3b） | G2.8：一句话需求跑完“查状态 → 出图 → 等待 → 决定 → 出视频”，视频期间续跑暂存、结束自动继续；重启可恢复 | **下一步** |
| **5 可安装包 + 配置向导** | 一键安装、Web 配置向导 + 设置页、安装文档、launchd 常驻、升级 / 卸载（见 §6d） | G5：干净 Mac 上仅按安装文档 30 min 内出第一张图（不含模型下载）；重启自动恢复 | **推后**（用户 2026-10-26 决定，先做 W2） |
| 6 在线模型与外部 agent | OpenAI / Anthropic 后端、aco_lw MCP 服务、Codex / Claude Code 接入、预算与隐私开关（见 §6c-4） | G6：本地 / 在线切换不丢会话与记忆；断网回退；agent 不越过 MCP 白名单 | 规划中 |

G0 结论（详见 `docs/baseline.md`）：LLM 加载仅约 15 s；视频峰值约 54 GiB，与 LLM 共存会进入内存压力警告档 → **默认提交视频前卸载 LLM，图片任务不卸载；LLM 空闲 5 min 用 `--ttl` 卸载；门控看系统可用内存 + 压力等级，不看进程 RSS**。原计划中 G0 要改写的两处默认值：**驻留策略**（切换耗时长 → 审阅会话期间生成模型常驻）和**档位上限**（峰值内存紧 → 严格错峰、视频档位封顶）。

## 5. 阶段 0 任务清单

- [x] **T0.1 内存采样器** `bench/memsample.py`：后台每 0.5 s 采 `vm_stat`/`memory_pressure`、ComfyUI 与 LM Studio 进程 RSS（psutil），写 CSV；给出峰值与 swap 变化。
- [x] **T0.2 LLM 加载/卸载耗时** `bench/llm_load.py`：`lms load qwen3.8-27b-splash` → 首 token → `lms unload`，各 3 次；记录加载后占用、量化格式；核实 `--ttl` 空闲卸载是否按实例生效。
- [x] **T0.3 ComfyUI 释放确认** `bench/comfy_free.py`：队列空闲时 `/free`，对照释放前后 RSS 与系统可用内存（复测试验报告的 9.3 → 0.82 GiB）。
- [x] **T0.4 图片冒烟 + 切换耗时**：01（FLUX.2 Klein），1280×720，冷启动 vs 热启动各 1 次。
- [x] **T0.5 视频冒烟**：04 文生视频（FreeVideo / MiniMax H3，不依赖定妆图）640×384、3 s、two_pass 8+3 步，**一次**；记录峰值内存、总耗时、各阶段耗时。✅ 用户已同意（虚拟机开着）。
- [x] **T0.6 共存测试**：27B 已加载 vs 已卸载各跑一次 T0.5，比较耗时与 swap（FreeVideo 会自适应，关注的是变慢多少）。
- [x] **T0.7 汇总** `docs/baseline.md`：表格 + 结论（驻留策略、档位上限、是否必须严格错峰）。

## 6. 阶段 1 任务清单

- [x] **T1.1 项目骨架**：uv + Python 3.11；依赖 `httpx`、`pydantic`、`typer`、`psutil`；`ruff` + `pytest`。
- [x] **T1.2 ComfyUI 客户端** `comfy.py`：同步 httpx，超时与重试；单测用录制的响应。
- [x] **T1.3 模板注册表** `templates/<name>/{api.json, slots.yaml}` + `registry.py`：slot 定义（节点 ID、字段、类型、范围、默认值）；导入 01/03/04/Z-Image。
- [x] **T1.4 档位与 schema** `presets.py`、`schema.py`：pydantic 模型；拒绝越界与未知字段；`/object_info` 节点校验（缓存）。
- [x] **T1.5 渲染** `render.py`：模板 + 参数 → 可提交 prompt JSON（纯函数，可单测）。
- [x] **T1.6 存储** `store.py`：SQLite（tasks 表：id、state、template、params_hash、prompt_id、时间戳）+ 任务目录 `manifest.json`；状态迁移函数集中在一处并校验合法迁移。
- [x] **T1.7 内存门控** `memgate.py`：系统可用内存 + `kern.memorystatus_vm_pressure_level` vs 模型注册表峰值（G0：视频 54 GiB）+ 8 GiB 余量；不足时依次 `/free`（仅空闲）→ 卸载 LLM → 排队等待。
- [x] **T1.8 调度器** `scheduler.py`：文件锁 → 查重（params_hash + 未完成任务）→ 门控 → `/prompt` → 轮询 `/history`（超时）→ 下载产物 → 写 manifest → 释放策略。
- [x] **T1.9 恢复** `recover.py`：启动时扫描 `已提交/生成中` 任务，按 prompt_id 对账；`/history` 与 `/queue` 都找不到 → 标记失败，**不自动重提**。
- [x] **T1.10 CLI** `cli.py`：`va submit/status/release/run`；`plan` 先返回“未启用”。
- [x] **T1.12 Web 进度台** `web/`：FastAPI 只读 API + 单页（原生 JS + SVG DAG），每 3 s 轮询；产物经本地路由预览。
- [x] **T1.13 进度文档** `docs/progress.html`：阶段 / 任务 / 闸门状态，Web 进度台内可直接打开。
- [x] **T1.11 G1 验收**：跑一张 1280×720 图；在“生成中”时 kill 进程 → 重启 → 只对账不重提；产物与 manifest 齐全。

## 6b. 阶段 2 任务清单：Telegram + 审阅流程（G2）

**目标**：人在手机上完成“出一组候选 → 选 / 微调 / 打回 → 锁进基准包”；等待人审阅期间不做任何 LLM 推理。

**新概念**
- **候选轮（round）**：一次需求生成 N 个候选（同参数不同 seed，图片默认 preview 档），作为一个审阅单位。任务表加 `round_id`；DAG 以轮为分组。
- **常驻服务 `va daemon`**：一个进程内跑三件事——Worker（串行消费 `draft` 任务，即现有编排器）、Telegram 长轮询监听器（不加载模型）、Web（进度台 + 审阅台）。CLI 仍可用，与 daemon 共用串行锁。
- **项目（project）**：最外层分组，一个创作目标一个项目（如“金发学院少女 · 回头镜头”）；轮、任务、基准包、对话都挂在项目下。项目可标记完成并**永久保留**，控制台可浏览所有历史项目（含已完成）。
- **对话历史**：所有入站 / 出站消息（Telegram、Web 审阅操作、LLM 规划回复、系统通知）落库到 `messages` 表，按项目查看。
- **导演基准包（baseline pack）**：版本化的已锁定资产（角色参考图、风格参考、视频运动参数），每次锁定生成新版本；后续任务可以 `@pack:角色名` 引用。

**任务**
- [x] **T2.1 候选轮**：`rounds` 表 + `va round <template> -n 4`；一轮的 N 个任务串行跑完后整轮进入“待审阅”。
- [x] **T2.2 Worker / daemon**：`draft` 即排队；daemon 循环取任务执行；`/pause` `/resume`；启动时先 `recover`。
- [x] **T2.3 审阅动作（编排器层，确定性）**：选中 → 锁定（种子、参数、产物）进基准包；微调重抽 → 固定种子 + 参数增量开新一轮；打回 + 批注 → 交给 T2.6 翻译成参数增量后开新一轮。“混合 A+B”依赖双参考模板，推迟到阶段 3。
- [x] **T2.4 基准包**：`packs` 表 + `data/packs/<name>/v<N>/`；`va pack show/lock/history`；任务参数可引用基准包里的图片。
- [x] **T2.5 Telegram 监听器**：白名单 chat_id；命令 `/status /tasks /pause /resume /cancel /mode /pack` 直接执行、不唤醒 LLM；一轮候选以**文件**方式发送（不压缩）+ 内联按钮（选 1…N / 微调 / 打回）；生成完成通知。
- [x] **T2.6 LLM 规划器（按需唤醒）**：自由文本 → 先秒回“收到”→ `lms load --ttl 300`（G0：约 15 s）→ 输出 JSON 参数增量 → **按模板槽位 schema 校验**，不合法则回问；上下文从数据库重建（基准包 + 最近几轮），不重放聊天；生成占内存时自由文本排队。
- [x] **T2.7 审阅台（Web，可操作）**：一轮候选网格并排、与基准包左右对照、键盘快捷键（1–9 选中，F 微调，R 打回），手机可用；所有按钮调用与 Telegram 相同的编排器函数。
- [x] **T2.8 驻留模式 `/mode`**：按需唤醒（默认）/ 会话常驻 / 纯命令；内存门控优先于模式。
- [x] **T2.10 项目与历史**：`projects` 表（进行中 / 已完成 / 归档）；`/project new|use|done`；控制台新增“项目”视图：项目列表（含已完成）→ 项目详情（轮次时间线、DAG、产物、基准包版本、对话历史）。
- [ ] **T2.9 G2 验收**（2026-10-26：控制台侧已走通“需求 → 4 候选 → 打回+批注 ×2 → 新一轮”；Telegram 白名单与推送已配好，**手机侧选版待用户测试**）：手机上收到一轮 4 个候选 → 选中 1 张锁进基准包 → 发一句批注 → 新一轮按批注改参数；日志证明审阅等待期间 LLM 未推理（已按 TTL 卸载）。

**本阶段不做**：一致性评分、探索预算、混合 A+B、OpenClaw。

## 6c. 待办（来自 2026-10-26 用户测试）

- [x] **B1 对话附参考图**：控制台对话框支持上传 / 粘贴本机路径，Telegram 支持直接发图；规划器据此选择参考编辑（`flux2-edit`）或图生视频，并可一键锁进基准包。
- [x] **B2 自由文本默认归属最近待审阅轮**：有待审阅轮次时，不点按钮直接打字 = 对最近一轮的“打回+批注”（DAG 连到上一轮、上一轮自动关闭）；以“新需求：”开头才开新线。
- [x] **B3 规划器上下文**：附带上一轮的批注链与用户原话，减少“太小”这类歧义；回复里复述理解的要点，便于用户纠正。
- [x] **B4 规划耗时**：根因是前缀缓存没命中（已由 S8 解决：多轮复用 55–73%，首 token 2–3 s）+ 思考输出 600–1500 token。实测关思考：`reasoning_effort` / `enable_thinking` 对该模型无效；`/no_think` 14.4 s → 9.3 s，做成可选 `planner_fast`（默认关，保理解质量）。

## 6c-2. 阶段 2.5：会话（session）与项目记忆（用户提议，2026-10-26）——已完成并合并到 main（2026-10-26）

**现状**：没有 session 概念。每次调用 LLM 都是无状态的，上下文从数据库临时拼出：项目名 + 基准包角色 + 最近 3 轮的 prompt（各截 300 字）+ 本条消息。**对话历史本身不进 LLM**，所以“刚才说的”“上一版”这类指代经常丢；项目里积累的偏好（如“35mm、电影感、精致 CG 偏真人”）也没有地方沉淀。

**目标结构**：项目 → 多个会话；项目记忆在会话之间共享。

| 层 | 内容 | 生命周期 | 谁写 |
|---|---|---|---|
| **项目记忆** | 角色设定、风格结论、用户偏好、禁忌、关键决定（各条附来源：消息 / 轮次 ID） | 跨会话永久；版本化，可回滚 | LLM **提议**、用户确认后写入（防记忆污染，见设计稿评审 #8）；用户也可手动编辑 / 置顶 |
| **会话摘要** | 本会话到目前为止的滚动摘要 | 随会话 | 接近上下文上限时自动压缩（LLM 总结较早的消息） |
| **会话近期消息** | 最近 K 条原话（含 LLM 回复与审阅动作） | 随会话 | 自动 |
| **实时状态** | 基准包、待审阅轮、最近几轮的 prompt 与决定 | 实时从数据库取 | 系统 |

**每次调用 LLM 的上下文**（按缓存友好顺序，见 S8）= 系统规则 + 模板说明 + 项目记忆 + 会话摘要 + 会话消息 + 实时状态 + 本条消息；按 token 预算组装（目标 ≤ 16k，27B 上下文 131k，留足思考余量，也控制耗时）。

**任务**
- [x] **S1 数据**：`sessions` 表（项目、标题、创建 / 最后活跃、摘要、状态）；`messages` 加 `session_id`；`memories` 表（项目、内容、类别、来源、状态：提议 / 生效 / 归档、版本）。旧消息归入各项目的“历史会话”。
- [x] **S2 会话操作**：控制台项目页左侧列出该项目的会话，可新建 / 继续 / 重命名；Telegram `/session new [标题]`、`/session list`、`/session use N`；默认继续该项目最近活跃的会话。
- [x] **S3 上下文组装器**：按上表组装、按预算截断，并记录每次调用用了哪些记忆条目（可追溯）。
- [x] **S4 自动摘要**：会话估算 token 超过阈值（如 12k）时，把较早的消息压缩进会话摘要；原消息保留在历史里，只是不再进 LLM。
- [x] **S5 项目记忆提议**：每轮“通过 / 打回+批注”之后，LLM 提议 0–3 条记忆（例如“年龄感：约 19 岁，精致 CG 偏真人；镜头保持 35mm”）；控制台与 Telegram 显示“记住 / 忽略”按钮；生效的记忆进入之后所有会话。
- [x] **S6 记忆管理页**：项目页内查看、编辑、置顶、归档、回滚记忆。
- [x] **S8 Prompt caching（用户要求，2026-10-26 实测有效）**：LM Studio 对该 27B 会复用前缀 KV 缓存——17.4k token 上下文冷算约 48 s，前缀相同只换结尾 **0.3–0.5 s**；开头改一个字即整段重算；引擎同时保留多份缓存（切回旧前缀仍命中）。设计要求：
  - **稳定前缀排序**：系统规则 → 模板说明（静态）→ 项目记忆（少变）→ 会话摘要（压缩时才变）→ 会话消息（只追加）→ **最后**才放实时状态（待审阅轮、内存等易变内容）与本条消息。时间戳、计数等易变内容不得出现在前缀里。
  - **只追加**：会话消息按原样追加，不改写历史；摘要压缩是唯一的前缀重写点，所以阈值要高、频率要低，压缩后立即预热。
  - **预热**：LLM 因视频被卸载、恢复加载后，后台用当前会话前缀发一次 `max_tokens=1` 的请求预热，用户下一句话不再等 48 s。
  - **会话活跃时延长 TTL**：缓存只在模型加载期间有效；活跃会话期间 TTL 延长到 20 min（可配置），空闲后照常卸载。
  - **可观测**：每次调用记录 prompt_tokens、首 token 时间、是否疑似命中缓存（首 token 远小于 prompt_tokens / 360 tok/s），在对话气泡上显示。
  - 将来若接云端模型（例如 Claude API），同一排序直接对应 cache_control 断点：规则 + 模板 | 项目记忆 | 会话摘要 + 消息。
- [x] **S9 规划器后端接口**：`PlannerBackend.chat(messages, schema) → (文本, 统计)`；会话 / 记忆 / 缓存逻辑与后端无关。本阶段实现 `lmstudio`（含加载 / TTL / 预热）与通用 `openai_compatible`（base_url + api_key + model），为阶段 6 铺路。
- [x] **R2 视频未实测档位**（用户反馈，2026-10-26）：规划器给视频选了未实测的 hd 档时，不再报错终止，改为自动用已实测档位排队，并提供“也试一下 hd（未实测）”按钮；规则文件加“视频默认用已实测档位”。
- [x] **R1 审阅结果可更改（用户要求，2026-10-26）**：已决定的轮 / 任务可“更改审阅结果”回到待审阅；旧决定进决定历史；若曾锁进基准包，新建版本把该角色恢复为之前的资产（版本不可改写）；旧决定派生的轮保留并提示可 /cancel。控制台按钮 + `/reopen`。
- [x] **I1 控制台双语（用户要求，2026-10-26）**：界面文字全部走词典（en / zh-CN），**默认英文**，右上角切换，选择记在浏览器；新增配置 `language`（默认 `en`）作为服务端默认。
- [x] **I2 服务端文字双语**：状态名、系统回复、Telegram 消息、错误提示按 `language` 输出；规划器回复语言跟随用户这句话的语言（规则文件里约定）。
- [x] **I3 文档默认英文**：面向外部的文档（README、运行手册、安装文档、进度页）英文为主，中文版并列（`*.zh-CN.*`，控制台随语言切换）。**内部文档（PLAN、baseline、设计稿）保持中文，不翻译**（用户决定，2026-10-26）。
- [x] **S7 与 B2、B3 合并**：有待审阅轮时，打字默认作为对该轮的批注；规划器回复里复述“我理解的要点”，并引用用到的记忆。

**验收**：同一项目开两个会话，第二个会话里说“按之前定的风格出一版”，规划器能从项目记忆里拿到风格；长会话自动摘要后，仍能正确指代“上一轮的 #2”。

## 6c-3. 控制台手动微调 workflow（用户提议，2026-10-26）

两层，从安全到灵活：
- [x] **W1 参数编辑器（不经 LLM）**：在控制台任一任务 / 候选上点“手动微调”，打开该任务的参数表单（模板槽位：prompt、seed、steps、参考图、档位等，带范围校验）；改完提交即生成一轮新候选，DAG 连到原任务，记录为“手动微调”。改动是确定性的，等价于 `/round`。
- [x] **W2 打开完整 workflow（高级，对应设计稿 L4 探索）**（2026-10-26 完成）：实现为“导出 API workflow → 拖进 ComfyUI 画布（前端 loadApiJson 保留节点 ID）→ Export (API) → 导入”。核实结论：ComfyUI 工作流列表只认画布格式，因此**不写入 ComfyUI 的 user 目录**；diff 相对导出的任务、校验含悬空连线；试跑用图内取值 + 最低档；晋升写入 `data/templates/`，只标记试跑过的档位为已实测。实测：改 steps 4→6 + prompt，试跑 preview 档 10.8 s 通过。原草案如下：
  - “在 ComfyUI 中打开”：把该任务实际提交的工作流导出成文件（`data/jobs/<ID>/prompt.json` 已有），并复制到 ComfyUI 的工作流列表里（`user/default/workflows/aco_lw/` 子目录，不碰已有工作流），在 ComfyUI 画布里手动改节点、连线。**待核实**：当前 ComfyUI 前端能否直接载入 API 格式，否则需要同时保存画布格式。
  - “导回 aco_lw”：把改好的工作流导入为**候选模板变体** → `/object_info` 图校验 → 最低档试跑 → 用户确认后晋升为正式模板（可插拔，同模型注册表）。未晋升的变体只在该项目内可用。
  - 控制台内也提供只读的节点 / 参数视图（JSON + 槽位高亮），便于对照。

## 6c-3b. 阶段 2.8：Agent 运行时（tool call）+ Executor 接口 + 动态资源调度（用户提出，2026-10-26）

**参考**：vhotseat（wandermyz/vhotseat：本地 LLM 与 ComfyUI 轮流使用同一 GPU / 内存——加载前把关、运行中的工作绝不打断、批量提交减少交接、状态实时读取）；成熟 agent 的工具循环（OpenAI / Anthropic tool calling 协议、最大步数、人工审批中断、可恢复的检查点）。
**实测前提（2026-10-26）**：LM Studio 上 qwen3.8-27b 原生 tool calling 可用（会先 `get_status` 再 `queue_round`）；工具往返后前缀缓存仍命中（第二轮 1235 token 首 token 0.64 s）。

### A. Agent 运行时：LLM 发 tool call，编排器执行，结果按资源情况择机交回
- **循环**：LLM → tool_calls → 编排器执行（内部工具 / 以后的 MCP）→ 结果写库 → *调度器决定何时把结果交回 LLM* → LLM 继续 → … → 最终回复。
- **运行状态（持久化，可恢复）**：`running` → `executing_tools` → `waiting_resources`（结果已就绪，但内存紧 / 视频在跑，暂存不发）→ `waiting_event`（等渲染完成）→ `waiting_user`（需要你批准或回答）→ `done` / `failed` / `cancelled`。每一步（assistant 的 tool_calls、每个 tool 结果）先落库再继续；daemon 重启后从最后一步接着跑。
- **工具分级**：
  - 只读（立即执行、同一轮内可缓存）：`get_status`、`list_rounds`、`get_round`、`list_templates`、`get_pack`、`list_memories`、`view_candidates`（以后用模型的视觉能力看图）。
  - 动作（经 Studio，带幂等键 = run+step+call_id，恢复时不会重复执行）：`queue_round`、`tweak`、`redo_round`、`reject_round`、`propose_memory`。
  - 长任务：`wait_for_round` → 运行进入 `waiting_event`，渲染完成后由事件唤醒并带回结果（产物、耗时、失败原因）。
  - 需要审批（进入 `waiting_user`，控制台 / Telegram 按钮）：`select_and_lock`（除非你在这句话里明确授权）、晋升 workflow、未实测视频档位；以后的 `install_lora`（L5）。
  - 外部（阶段 7）：`web_search` 等，带隐私开关。
- **结果交回策略**：交回 LLM 本身是调度器里的一个作业（LLM 推理），按资源决定时机；结果太长时截断 / 摘要后再交回，原文留库。
- **预算**：每次运行最多步数、最多渲染分钟数；超了停下来问你。
- **缓存**：assistant 的 tool_calls 与 tool 结果逐字存、逐字复现（与 S8 一致）。
- **兼容**：现有单次 JSON 规划保留为“简单模式”后端；纯命令模式不变。

### B. Executor 接口（替代编排器对 ComfyUI 的直接依赖）
- `Executor`：`estimate(task)`（预计峰值内存、耗时、需要的常驻模型、切换代价）· `submit` · `poll`（进度）· `collect` · `cancel`（只取消自己的）· `release`（释放模型）· `resident()`（当前常驻什么）· `health()`。
- 实现：`ComfyExecutor`（现有逻辑搬过来，行为不变）、`FFmpegExecutor`（拼接、抽帧、转码，本地子进程）；以后：独立 LTX 服务、其他本地生成器、在线生成服务（不占本地内存）。
- LLM 也是一个“工作负载”（Workload）：`load / unload / in_flight / resident`，与 Executor 一起由资源管理器调度（对应 vhotseat 的 driver）。

### C. 动态资源调度器（替代 `memgate` 的固定阈值）
- **实时资源视图**：可用内存、压力等级、swap 趋势；各工作负载当前常驻（LLM 是否加载、ComfyUI 最近加载的是哪个模型族）。
- **从历史学习的估计**：按（模板, 档位）用已记录的 metrics 做滑动平均——峰值内存、耗时、加载耗时；没有历史时回退到模型注册表。
- **统一作业队列**：渲染作业与 LLM 推理作业在同一个队列里排；优先级：用户交互 > agent 续跑 > 后台探索 / 试跑；等待时间老化防饿死。
- **减少切换**：同一模型族的作业成批连续执行（如 4 张图连跑，中间不插 LLM 推理）；切换代价（LLM 加载约 15 s + 缓存失效、图片模型冷启动 +12 s、视频模型加载）计入排序。
- **准入与释放计划**：预计可用（当前可用 + 可释放的）≥ 需求 + 余量才放行；需要时按计划释放（`/free`、卸载 LLM），**绝不打断正在运行的工作**（LLM 正在回答时等它答完；渲染中不抢）。
- **ETA**：控制台显示队列时间线与预计完成时间；资源时间线图（内存、压力、谁在用）。

**已定（2026-10-26）**：agent 可以自己查状态、排队出图、重做、按批注微调、等待渲染、继续下一步；**选版、锁进基准包、用未实测视频档位须你批准**（按钮），除非你在当句话里明确授权。tool-calling agent 为默认模式，`/mode simple` 切回单次 JSON 规划。

### D. 策略层（用户补充，2026-10-26）
**原则**：暂停的是 agent 对话，不是正在进行的推理；多个 tool call 必须按顺序、带原 call ID 逐个补齐结果。**由显式策略决定“要不要”以及“何时”把结果交回 LLM**；自动重试、降档、回滚都必须写在策略文件里，运行时不得自行改变任务的含义——策略没写的情况一律交回 LLM。

| 工具结果 | 下一步（策略配置，`policies.yaml`） |
|---|---|
| 渲染成功，且属于一个流水线 | 直接排队下一步，LLM 不参与 |
| 执行端队列拥堵 / 内存不够 | 调度器等待 |
| OOM | 按策略：同参数重试 N 次（显式），或降档（仅当策略列出了允许的档位） |
| 视频完成，需要评估 | 调度视觉评估（以后：视觉模型；现在：交给你 / LLM） |
| 评估不通过 | 交回 LLM 重新规划 prompt |
| 你提出新需求 | 优先级提升，等合适的模型执行窗口 |

**执行策略**：每个工具的权限（允许 / 需审批 / 禁止）、参数上限（如每轮最多候选数）、工作目录、网络访问；shell / MCP / 联网工具默认禁止，开启时限定白名单。

**MVP 对照**：Runtime 核心 = SQLite（会话 / 任务 / 工具结果 / 事件）· 任务调度器（优先级、依赖、重试、唤醒条件）· 资源管理器（内存压力、GPU、并发）· LLM 适配器（LM Studio / Codex / 云端 API）· 工具注册表（ComfyUI / Shell / MCP）· 执行策略（权限、参数、目录、网络）。

### 任务
- [x] **E1** Executor 接口 + `ComfyExecutor`（重构，行为不变，测试全过）+ LLM Workload
- [ ] **E2** `FFmpegExecutor`（抽帧、拼接、转码）
- [x] **R1** 资源视图 + 从历史 metrics 学习的估计（峰值、耗时、加载耗时）
- [x] **Q1** 统一作业队列与调度器（优先级、按模型族成批、切换代价、准入与释放计划、不打断运行中的工作），替代 Worker 主循环与 memgate
- [x] **A1** 工具注册表（JSON schema、分级、幂等、审批）+ 运行与步骤持久化
- [x] **A2** Agent 循环：tool call 执行、结果暂存、经调度器择机交回 LLM、事件唤醒、预算、崩溃恢复
- [ ] **A3** 渠道接入：控制台与 Telegram 显示运行步骤（调用了哪些工具、结果、在等什么）、审批按钮
- [ ] **U1** 控制台：队列时间线 + ETA、资源时间线图、运行详情
- [ ] **P1** 续跑策略（`policies.yaml`）：按工具结果类型决定 交回 LLM / 自动下一步 / 有限重试 / 等待 / 通知你
- [ ] **P2** 执行策略：工具权限、参数上限、审批规则、目录与网络；在执行每个工具前检查
- [ ] **D1** 流水线工具 `queue_pipeline`：多步顺序执行（`@prev` 引用上一步产物），步与步之间按策略自动推进，不唤醒 LLM；为分段长视频（FreeVideo continue_from + FFmpeg 拼接）铺路
- [ ] **T1** 测试：假 Executor / 假 LLM 覆盖竞态（渲染中 LLM 请求、LLM 回答中渲染到达、重启恢复、重复执行防护）
- **验收 G2.8**：一句话需求 → agent 查状态、排队一轮图、等渲染、拿到结果后再决定下一步（例如出视频）；视频渲染期间 agent 的续跑被暂存、渲染结束后自动接着跑；全程无内存压力严重档、无重复提交；daemon 中途重启后运行能接着走完。

## 6c-4. 阶段 6（草案）：在线模型与外部 agent（Codex / Claude Code 等）

**原则不变**：LLM / agent 只产出数据或调用 aco_lw 的白名单命令；一切副作用仍在编排器。外部 agent 拿不到 ComfyUI 与 shell（设计稿 Concern #4）。

两种接入位置：
1. **规划器后端**（被动）：`openai`、`anthropic` API，或非交互的 agent CLI（`codex exec` / `claude -p`，强制 JSON schema 输出、只读沙箱、无工具）。接口同 S9。
2. **操作员**（主动）：aco_lw 提供 **MCP 服务**，只暴露 `round / review / status / release / project / memory` 等工具；Codex、Claude Code 等作为客户端驱动创作循环（可选看图审阅）。agent 的工具白名单只含这些 MCP 工具。

| 影响 | 处理 |
|---|---|
| 内存：不再需要视频前卸载本地 27B（省 19.5 GiB） | 模型注册表 `exclusive_llm` 只对本地后端生效 |
| 时延：无加载、无本地 prefill | 沿用 S8 排序，映射到云端缓存断点（Anthropic `cache_control`；OpenAI 自动前缀缓存） |
| 成本：按 token 计费 | 每项目 / 每日预算上限，超出降级到本地或纯命令；调用记录含用量与费用 |
| 隐私：需求、项目记忆、参考图上云 | 按项目开关“允许上云”与“允许发送图片”；默认关闭 |
| 安全：agent CLI 自带文件 / shell 能力 | 只读沙箱 + 仅 MCP 工具；输出一律 schema 校验 |
| 离线 / 故障 | 自动回退到本地 27B，再回退到纯命令模式 |

**任务**：
- [x] O1 Anthropic 后端（原生工具调用、Claude 回合原样回放、缓存断点、服务端拒答回退）+ 通用 OpenAI 兼容后端（2026-10-27）
- [x] O2 aco_lw MCP 服务（stdio，只暴露 aco_lw 工具，调用转发给 daemon 执行）
- [x] O3 Claude Code / Codex 委托：Claude Code `--tools ""` + 仅 aco_lw MCP；Codex 只读沙箱；空工作目录
- [x] O4（部分）回退配置：`llm_fallback` 顺序 + `allow_online` 隐私开关（默认关）；本地资源不够或不可用时启用。待做：按项目选择、预算上限、设置页
- [ ] O5 视觉审阅（可选：在线模型对候选写差异说明，仍不自动淘汰）
**验收 G6**：同一项目在本地与在线后端之间切换，会话与项目记忆不丢；断网自动回退；agent 无法越过 MCP 白名单。

## 6c-5. 阶段 7（草案，用户 2026-10-26 提出，放在后面）：联网资料与 LoRA

**现状**：规划器不是能自己调用工具的 agent，只输出一次受约束的 JSON；不能上网，也没有任何工具。

用途：找参考图、查风格与镜头语言的描述、看模型 / LoRA 资料 → 选择 → 安装 → 试验。

- [ ] **N1 只读搜索工具**：规划器多一个 `search` 动作（返回查询词），编排器代为搜索（需选搜索服务与 key），结果作为数据回填，规划器再决定；受控两步循环，次数受预算限制。需求文字会发给搜索服务（隐私开关）。
- [ ] **N2 参考图收集**：搜索结果中的图片可一键存为 `@upload` 参考，带来源链接；用于参考编辑 / 风格探索。
- [ ] **N3 LoRA 资料与选择**：查 LoRA 说明（触发词、推荐权重、底模兼容性），整理成候选清单给用户选。
- [ ] **N4 LoRA 安装（设计稿 L5：每次都要人确认）**：用户点确认后才下载到 ComfyUI 的 `models/loras/`，校验哈希与大小；这会突破当前“不下载模型”的硬约束，届时需在 CLAUDE.md 中改为“经用户逐次确认可下载 LoRA”。
- [ ] **N5 LoRA 试验**：用 W2 的变体机制生成“加 LoRA 节点”的 workflow 变体 → 最低档对照试跑（有 / 无 LoRA、不同权重）→ 用户确认后晋升。
- 实现路径二选一或组合：阶段 4（OpenClaw 子 agent，复用其搜索 / Skills）或阶段 6（在线 agent 经 MCP 操作 aco_lw）。

## 6c-6. 阶段 3（改版）：制片流水线——小说到成片（用户提出，2026-10-27）

用户原话：给出一段小说文字，规划成分镜剧本，定妆主要人物、场景，镜头分镜、运镜细节，规划视频与一致性，生成视频、剪辑、合并出片；中间经审核通过才进入下一轮。参考 ArcReel，但**专注基础架构和扩展性**，内部功能尽量用已有的 skill，目标是可扩展、调度有效。

### 参考 ArcReel 得到的结论（AGPL-3.0：只借思路，不拷代码）
- **服务端权威计划**：`get_workflow_plan` 返回 steps / next_action / 各操作准入；agent 不自建状态机、不凭文件或对话记忆判断进度。→ 我们照做，而且计划是确定性代码算出来的。
- **产物时效**：每类产物分 current / stale / missing；改上游只让依赖它的下游过期。→ 照做，用输入指纹（revision hash）判定。
- **主 agent 只编排**：长原文不进主 agent 上下文，由子 agent / skill 自己读文件，只传路径和参数。→ 照做：Skill 执行端读 artifact 路径。
- **每步确认协议**：继续 / 重做（带修改意见）/ 跳过。→ 复用现有审阅（选版 / 重做 / 打回 / 重开）。
- **LLM 只产出视觉层、schema 强约束、违约落草稿而不是丢弃重抽**。→ Skill 输出一律 JSON Schema 校验，失败存 `*.invalid.json` + 违约清单。
- 我们与它的差异：本地优先 + 内存调度（它是在线 API 为主）；工作流是数据（YAML），不是写死在代码和 profile 里的六种模式组合。

### 架构（新增 5 个通用部件，小说只是其中一套工作流）
1. **工作流规格（`workflows/*.yaml`）**：阶段 DAG。每个阶段声明 `kind`（skill / render / tool）、输入产物、输出产物类型与 schema、`fan_out`（按角色 / 场景 / 镜头展开）、`gate`（review / auto，策略可覆盖）、使用的 skill 或模板与档位。新增“广告片”“MV”= 新 YAML + 几个 skill，不改代码。
2. **产物库（artifacts）**：带类型、版本、谱系（输入指纹）的产物——剧本、人物设定卡、场景设定、分镜表、关键帧、镜头视频、剪辑时间线、成片。改一个人物 → 依赖它的关键帧 / 镜头自动标 stale；旧版本保留可回滚。复用现有 SQLite 与 `data/`。
3. **计划引擎（权威，确定性）**：由规格 + 产物算出每阶段状态（completed / ready / active / review / blocked / stale）、`next_action`、各操作准入、整片 ETA。agent 工具新增 `get_production_plan` / `run_stage` / `review_stage`；控制台、Telegram、Claude Code 看到的是同一份计划。
4. **SkillExecutor（Executor 接口的新实现）**：加载 Claude Code 格式的 `SKILL.md`（frontmatter + 说明 + references + 可选脚本），来源：内置 `skills/`、用户 `data/skills/`、可选外部目录（如 `~/.claude/skills`）。阶段规格指定 skill 与输出 schema；执行者按策略选：本地 27B（短输入）或委托 Claude Code / Codex（长原文、复杂改编），都只走 aco_lw MCP 工具。skill 内的脚本只在编排器沙箱里跑（只读输入、写 artifact 目录，不联网）。
5. **FFmpegExecutor（E2）**：裁切、拼接、转场、调速、字幕烧录、混音、末帧抽取；CPU 资源类，可与渲染并行。剪辑由 skill 产出时间线（EDL JSON），FFmpeg 只执行时间线——LLM 不直接拼命令行。

### 调度（在现有 ResourceManager 上扩展）
- 阶段任务带资源类：`llm-local` / `llm-online` / `render-image` / `render-video` / `cpu`。
- **按模型族批量**：同一阶段内先把所有关键帧（FLUX）跑完再切视频（H3），避免来回换模型；`cpu`（FFmpeg）不占闸门、可与渲染并行。
- 展开后的镜头按片中顺序 + 审阅等待情况排优先级：先出前几镜给你审，后面的边审边跑（审阅不阻塞后续渲染，但打回会让依赖它的已排队任务撤回）。
- 整片 ETA 用学习到的 exec 时间：例如 8 镜 × smoke 3 s（实测 329 s / 条）≈ 44 min 视频渲染 + 关键帧 8 × 27 s（实测，standard 档）≈ 4 min（均为**估算**汇总）。

### 小说 → 成片工作流（第一套 YAML）
| # | 阶段 | 类型 | 产物 | 审核 |
|---|---|---|---|---|
| 1 | 导入原文 | tool | source（文本） | — |
| 2 | 改编：梗概、场次划分 | skill | adaptation.json | 审 |
| 3 | 设定圣经：人物 / 场景 / 道具 / 美术风格 | skill | bible.json | 审 |
| 4 | 定妆：人物设定图、场景基准图（每项 n 张候选） | render（展开） | character / scene sheets | 选版锁定 |
| 5 | 分镜脚本：每镜景别、构图、运镜、时长、台词、引用哪些设定 | skill | shotlist.json | 审 |
| 6 | 关键帧（以定妆图为参考） | render（展开） | keyframe / 镜 | 选版 |
| 7 | 镜头视频（I2V，运镜写进 motion prompt） | render（展开） | clip / 镜 | 选版 / 重做 |
| 8 | 剪辑：顺序、裁切、转场、节奏 | skill → 时间线 | timeline.json + 粗剪 | 审 |
| 9 | 合成成片（可选字幕 / 配乐） | tool（FFmpeg） | final.mp4 | 审 |

### 一致性方案（分级）
- 现在就能做：设定圣经写进项目记忆；每个人物锁定的设定图 + 固定种子；关键帧用 `flux2-edit` 以人物设定图为参考；镜头视频从已选关键帧 I2V；连续镜头用上一镜**末帧**接续（新增 `@task:<id>#last`）。
- 缺口：`flux2-edit` 目前单参考图，“人物 + 场景”同时参考需要多参考 workflow——需要你从标准实验室提供（我们不装节点）；人脸 / 风格一致性评分放到后面。

### 里程碑
- [x] **3a 基础架构**（2026-10-27 完成：`production.py` 计划引擎与产物库、`skills.py`、`executors/ffmpeg.py`、`workflows/novel-to-film.yaml` + 4 个 schema + 4 个内置 skill、agent 工具 5 个、Web API、控制台「制片」视图；测试 9 个）：工作流规格 + 产物库（版本 / 过期）+ 计划引擎 + SkillExecutor + FFmpegExecutor + 通用阶段审核；用一个 2 阶段玩具工作流和假执行端做测试。
- **3b 小说 → 成片**：第一套 YAML + 内置 skill（改编、设定圣经、分镜、剪辑）；smoke 档跑一段短篇（真实渲染前征求确认）。
- [~] **3c 控制台“制片”视图**（最小版已做：阶段流水线、条目缩略图、开跑 / 通过 / 重做 / 打回 / 保留 / 手改 JSON、成片播放；分镜板与时间线预览待做）：阶段 DAG（复用 Flow）、分镜板（镜头 × 关键帧 / 视频）、时间线预览、逐阶段审核；Telegram 推送每阶段审核。
- **3d 一致性增强**：多参考模板（待你提供 workflow）、末帧接续、配音 / TTS（另议）。

### 3.1 多来源输入 + 自带定妆（用户 2026-10-27 提出；同日完成：`ingest.py`、tool 阶段、alternatives、provide、控制台与 agent 入口；实测维基百科网页 + YouTube 元数据 / 字幕 / 缩略图 / 2 帧 4.9 s）
目标：制片的输入不只是一段小说——可以是自然语言描述 + 图片、网页链接、YouTube 链接；可以直接用自己提供的定妆照。
1. **输入是一组来源**：`text`（小说或一句话构想）、`image`（带名字 / 角色：人物 · 场景 · 风格 · 道具，用法：原样使用 / 按风格重画）、`url`（网页正文 + 主图）、`youtube`（标题、简介、章节、字幕、缩略图，可选抽 N 帧）。
2. **新阶段类型 `tool`**：确定性的编排器工具（可注册、可扩展）。新增 `brief` 阶段（tool: ingest），把所有来源整理成一份“素材包”（正文 + 参考图清单 + 来源状态），之后的 skill 只读素材包。抓取在编排器里做（有超时、大小上限，不执行页面脚本）；失败的来源标出原因，不阻塞其余来源。
3. **自带定妆**：设定圣经给人物 / 场景挂上 `reference`（素材包里的参考图 ID）；渲染阶段支持数据里写的条件分支（`alternatives: when … → provide / render`）：
   - 原样使用 → 直接作为已通过的定妆图（不渲染）；
   - 按风格重画 → FLUX.2 以你的照片为参考编辑出定妆图。
   另外任何渲染条目都可以手动“用我的图片”替换（控制台条目菜单 / API），视为已通过；下游照常按依赖过期。
4. **入口**：控制台新建制片支持多行链接 + 多图上传（每张图填名字、类型、用法）；agent 工具 `create_production` 接受 `sources`；Telegram 发的照片已是上传，可直接引用。
5. 依赖：`trafilatura`（网页正文）、`yt-dlp`（YouTube 元数据 / 字幕 / 缩略图 / 低清视频抽帧）。不下载模型。

### 3.2 一致性与 ComfyUI 智能控制（用户 2026-10-27 决定）
背景：「马尔福小姐」试片里关键帧与视频都没保住定妆脸，双人镜头特征串了。根因：视频只靠“首帧 + 一句话”，H3 提示词不合官方格式，FLUX 单参考。
决定：
1. **视频一致性走 FreeVideo 角色卡 + 电影**：新增 FreeVideo 执行端（本机 `/freevideo/agent/v1`，token 只在进程内读取、不打印）。人物 / 场景 → FreeVideo 角色卡（最多 6 张标注图 + 声音）；镜头视频 → FreeVideo 多镜头电影（plan → start → 每镜 takes → 我们的审阅里选 → 续拍用上一镜选中 take 的末帧 → finish）。暂停 / 审核仍由我们控制（电影的暂停只有用户能在 FreeVideo 页面操作：我们暂停时不再推进 choose / finish）。
2. **agent 的 ComfyUI 权限**：可以探索模型、节点、你的 workflow，搭建与修改 workflow，按最低档试跑（经编排器、受暂停与资源闸门）；**安装节点 / 下载模型仍需你批准**。
3. **接 artokun/comfyui-mcp，但由我们的编排器调度**：编排器作为 MCP 客户端启动它（stdio），把它的工具按类别登记进我们的工具表与策略：读（模型 / 节点 / workflow 查询）直接放行；搭建（改图、校验）放行；执行类调用改为经编排器提交成我们的任务（可追踪进度、受暂停与资源调度）；安装类需要审批。
4. **装 MiniMax 官方 `h3-prompt-writing` skill**（data/skills），视频提示词按 H3 官方格式（英文分段、`<d>[English] …</d>` 台词、说话人 ID、镜头切换时间）。
5. 这部片全英文（英式英语）。
顺序：H3 skill + 英文 → FreeVideo 角色卡（马尔福用你的 7 张高清图）→ FreeVideo 电影执行端接镜头视频阶段 → comfyui-mcp 由编排器调度 → 关键帧双参考。

- 2026-10-27 用户要求：FreeVideo 的批准开关 `ComfyUI/output/FreeVideo/.agent/confirm.json` 设为 `{"start": false, "reshoot_seconds": null}`（开拍、重拍不再在 FreeVideo 页面询问；我们的「开始」按钮与审核照旧）。

### 已定（用户 2026-10-27）
1. skill 来源：内置 + 兼容 Claude Code SKILL.md 格式的外部 skill（`data/skills/`，按阶段映射）；不默认扫描 `~/.claude/skills`。
2. 改编 / 分镜的 LLM：可配置（阶段级），默认本地 27B；本地被视频占用时按 `llm_fallback` 回退。
3. 第一版成片无声 + 可选字幕（FFmpeg 烧录台词）；配乐 / 配音放到 3d。

## 6c-7. 通用 agent（用户 2026-11-07 决定；已完成，分支 `agent-general-tools`）

架构见 `docs/design.html` §6b。LLM 是大脑、只提出 tool call；编排器执行全部工具（生成工具 + 通用工具 `toolbox.py` + MCP `mcp_hub.py`），结果按内存情况择机交回。取代原“OpenClaw 子 agent 提供搜索 / Skills / MCP”（阶段 4）。

- [x] 通用工具：web_fetch、run_command（每条经批准，危险操作拒绝）、读写文件、skill（含 `~/.claude/skills`）、agent 记忆、add_media、send_file
- [x] MCP：stdio + Streamable HTTP；只读工具直跑，其余经批准；当前配置 Exa
- [x] 委托 FreeVideo agent：`/fv`、`delegate_freevideo`（Claude Code + FreeVideo MCP，每次经批准）
- [x] Z-Image 模板（`zimage-t2i` / `zimage-i2i`），定妆默认用它（Klein 4B 脸型不受控，实测 4 批）

与 Claude Code / OpenClaw 的使用差距（详见设计文档 §6b）与下一步：
- [x] 视觉：`look` 工具（本地 27B 是视觉模型）；图紧跟工具结果交给 LLM，Claude 后端自动转换
- [ ] 审批摩擦：按会话 / 任务一次性授权一类只读命令
- [x] 在线大脑：`/model local|openai|openrouter|anthropic|claude_code|codex`（需 `allow_online`，关掉自动退回本地）；`delegate_agent` 把子任务交给 Claude Code / Codex（每次经批准）
- [ ] 受控自扩展：agent 提出新模板 → 编排器校验 + 最低档试跑 → 用户批准登记

## 6d. 阶段 5（草案）：可安装包 + 配置向导（目标：新机器一键跑起来）

**目标**：在一台新的 Mac 上，按安装文档执行一条命令 → 打开浏览器里的配置向导 → 走完即可出图 / 出视频。

**已定（2026-10-26）**：只支持 Apple Silicon Mac；安装器只装 aco_lw，ComfyUI / FreeVideo / LM Studio / 模型由向导**检测 + 引导安装**（给命令或下载链接，装好自动识别）；形态为**一行命令 + launchd 后台服务**；配置向导为 **Web（首次进入 `/setup`，之后控制台“设置”页可改）**。

**范围**
- **安装器**：一条命令安装（`curl … | sh` → 检查 macOS / Apple Silicon / 内存 / 磁盘 → 安装 uv → 安装 aco_lw 到 `~/Applications/aco_lw` 或 `uv tool`）→ 注册 launchd 常驻（开机自启、崩溃重启）→ 打开向导。
- **配置向导（Web，首次启动自动进入 `/setup`）**：
  1. 环境体检：内存、磁盘、芯片、端口占用（自动避开已占用端口）。
  2. ComfyUI：探测已有实例 / 指定地址；检查所需自定义节点与模型文件，缺什么列什么。
  3. 模型与模板：按已安装的模型启用对应模板（可插拔，视频模型可换）；未实测档位标记为未验证，可在向导里跑一次冒烟实测并写回模型注册表。
  4. LLM：探测 LM Studio / 选择模型 / 测一次加载与规划；也可选“纯命令模式”不装 LLM。
  5. Telegram（可选）：填 token → 自动验证 → 发 `/whoami` 一键加入白名单。
  6. 数据目录、候选数、内存余量等偏好 → 写 `config.toml`。
  7. 完成：跑一张测试图，显示成功页。
- **设置页**：向导之后随时可在控制台“设置”里修改同样的配置（含重新体检）。
- **安装文档**：`docs/install.md`（前置条件、一键安装、向导截图、卸载、升级、常见问题）。
- **卸载 / 升级**：`aco_lw uninstall`（保留或删除数据二选一）、`aco_lw upgrade`。
- **验收 G5**：在一台干净的 Mac 用户账户上，仅按安装文档操作，30 分钟内（不含模型下载）出第一张图；重启后服务自动恢复。

## 7. 目录结构（目标）

```
video-agent/
├── PLAN.md                 # 本文件
├── docs/design.html        # 设计稿原件
├── docs/baseline.md        # G0 实测结论（阶段 0 产出）
├── bench/                  # 阶段 0 实测脚本
├── templates/<name>/       # api.json + slots.yaml
├── src/video_agent/        # comfy / registry / schema / render / store / memgate / scheduler / recover / cli
├── tests/
├── config.example.toml     # ComfyUI/LM Studio 地址、余量、释放策略
└── data/                   # gitignored：sqlite、jobs/<id>/manifest.json + 产物、bench CSV
```

## 8. 已定 / 待决

已定（2026-10-26）：
1. T0.5 视频冒烟可以跑；**虚拟机保持开启**，基线即日常状态。
2. 用户会在系统运行时手动使用 ComfyUI 网页 → 释放前二次确认队列，只释放本系统加载的模型。
3. 视频模型不固定（MiniMax H3 / LTX 2.5 / 更新的）→ 注册表可插拔；本轮冒烟用当前装好的 FreeVideo（MiniMax H3，04 文生视频）。
4. Web 进度台端口 8790（8765 是用户另一个 server）。
6. 阶段 5：Apple Silicon only；安装器只装 aco_lw，重依赖由 Web 向导检测 + 引导；一行命令 + launchd；Web 向导 + 设置页。
5. 阶段 2：用户新建专用 Telegram bot，token / chat_id 写进 gitignore 的 `config.toml`；**候选以原图文件发送**（角色虚构，可上云）；**手机只用 Telegram**，审阅台只在本机用（仍绑定 127.0.0.1）；每轮候选数可配置，默认 4。

待决：
- FreeVideo 自适应内存 → 内存门控判据要从“够不够”改成“可用内存 vs 耗时曲线”，由 T0.5/T0.6 的数据定。
- 实测前 swap 已用 12.7 / 14.3 GB（2026-10-26），基线需注明。
- 每日任务量、是否夜间批跑、是否需要 LLM —— G1 之后再定。
