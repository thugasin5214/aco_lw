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

1. LLM 和 Telegram **永远不直接**碰 ComfyUI API 或 shell；只能调编排器的 4 条命令。
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
| 3 短视频闭环 + 有限探索 | 样片、一致性评分、预算内多候选、档位逐级上调 | G3：遵守探索预算；未经选定不替换定妆 | 后续 |
| 4 扩展能力 | OpenClaw 子 agent：搜索、Skills、MCP | G4：按需启动、能退出、无孤儿进程 | 后续 |
| **5 可安装包 + 配置向导** | 一键安装、Web 配置向导 + 设置页、安装文档、launchd 常驻、升级 / 卸载（见 §6d） | G5：干净 Mac 上仅按安装文档 30 min 内出第一张图（不含模型下载）；重启自动恢复 | **下一步**（用户指定，先于阶段 3/4） |

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

- [ ] **B1 对话附参考图**：控制台对话框支持上传 / 粘贴本机路径，Telegram 支持直接发图；规划器据此选择参考编辑（`flux2-edit`）或图生视频，并可一键锁进基准包。
- [ ] **B2 自由文本默认归属最近待审阅轮**：有待审阅轮次时，不点按钮直接打字 = 对最近一轮的“打回+批注”（DAG 连到上一轮、上一轮自动关闭）；以“新需求：”开头才开新线。
- [ ] **B3 规划器上下文**：附带上一轮的批注链与用户原话，减少“太小”这类歧义；回复里复述理解的要点，便于用户纠正。
- [ ] **B4 规划耗时**：上下文变长后 23–41 s，评估关闭思考 / 缩短上下文。

## 6d. 阶段 5（草案）：可安装包 + 配置向导（目标：新机器一键跑起来）

**目标**：在一台新的 Mac 上，按安装文档执行一条命令 → 打开浏览器里的配置向导 → 走完即可出图 / 出视频。

**范围（待用户确认，见 §8 待决）**
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
5. 阶段 2：用户新建专用 Telegram bot，token / chat_id 写进 gitignore 的 `config.toml`；**候选以原图文件发送**（角色虚构，可上云）；**手机只用 Telegram**，审阅台只在本机用（仍绑定 127.0.0.1）；每轮候选数可配置，默认 4。

待决：
- **阶段 5 范围**：目标机器类型、是否由安装器负责安装 ComfyUI / FreeVideo / LM Studio 与模型、安装形态（命令行脚本 / .app / Homebrew）、向导形态。
- FreeVideo 自适应内存 → 内存门控判据要从“够不够”改成“可用内存 vs 耗时曲线”，由 T0.5/T0.6 的数据定。
- 实测前 swap 已用 12.7 / 14.3 GB（2026-10-26），基线需注明。
- 每日任务量、是否夜间批跑、是否需要 LLM —— G1 之后再定。
