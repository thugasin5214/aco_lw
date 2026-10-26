# aco_lw 运行手册（Runbook）

更新于 2026-10-26 · 适用版本：阶段 2（`va daemon` + 控制台 + Telegram）

本手册回答三件事：**怎么开 / 关**、**日常怎么用**、**出问题怎么查**。设计取舍见 `docs/design.html`，计划与进度见 `PLAN.md` / `docs/progress.html`，实测数据见 `docs/baseline.md`。

---

## 1. 组件与位置

| 组件 | 地址 / 位置 | 说明 |
|---|---|---|
| aco_lw 控制台 | http://127.0.0.1:8790 | `va daemon` 启动；只绑定本机。**8765 是另一个 server，不要占用** |
| ComfyUI（FreeVideo-local） | http://127.0.0.1:8188 · `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI` | 执行端：FLUX.2 出图、FreeVideo / MiniMax H3 出视频。aco_lw 不负责启动它 |
| LM Studio | http://127.0.0.1:1234 · CLI `lms` | 27B 规划器（`qwen3.8-27b-splash`），按需加载、空闲 5 min 卸载 |
| Telegram bot | @luwu_abot | 白名单 chat_id 见 `config.toml` |
| 代码 | `~/workplace/video-agent`（GitHub 私有仓库 `aco_lw`） | |
| 配置 | `config.toml`（gitignore，权限 600） | token 等敏感项只在这里 |
| 数据 | `data/` | `tasks.sqlite`（索引）、`jobs/<任务ID>/`（manifest + 产物）、`packs/<项目ID>/v<N>/`（基准包）、`daemon.log` |

## 2. 启动与停止

### 启动顺序
1. **ComfyUI**：确认 http://127.0.0.1:8188 能打开（FreeVideo-local 环境）。
2. **LM Studio**：应用开着即可；不需要手动加载模型，aco_lw 会按需加载。
3. **aco_lw**：执行下面的命令。

```sh
cd ~/workplace/video-agent
uv run va daemon 2>&1 | tee -a data/daemon.log
```

看到 `Telegram 监听已启动（白名单 [...]）` 与 `控制台：http://127.0.0.1:8790` 即就绪。启动时会自动对账（`recover`），只收集已完成的任务，**不会重复提交**。

### 停止
- 在运行 daemon 的终端按 `Ctrl-C`，或 `pkill -f "va daemon"`。
- 正在 ComfyUI 里跑的任务**不会被取消**；下次启动 daemon 会按 prompt_id 对账并收回产物。

### 只看不跑
`uv run va serve`：只开控制台（只读 + 审阅），不出图。排队任务要等 daemon 起来才执行。

## 3. 日常使用

### 3.1 一次完整的创作循环
1. **提需求**：控制台「项目与对话」对话框，或 Telegram 里直接说中文。例：“同一个金发学院少女，回头看向镜头的半身照，傍晚暖色窗光”。
   LLM 先秒回“收到”，约 10–40 s 理解后排队一轮候选（图片默认 4 张，视频 1 条）。
2. **等出图**：图片约 12 s / 张；视频 640×384×3s 约 5–6 min。完成后控制台「审阅」出现角标，Telegram 收到原图文件 + 按钮。
3. **审阅**（控制台 `#review` 或 Telegram 按钮，二者等价）：

   | 动作 | 效果 |
   |---|---|
   | ✅ 通过 | 选中该候选，其余标为打回；可同时**锁进基准包**（角色 / 场景 / 风格） |
   | 🎲 整轮重做 | 整轮不要，同参数换种子再出一轮 |
   | 🔧 微调 #N | 写一句修改说明；保留 #N 的种子，LLM 改写 prompt 后出新一轮 |
   | ↩️ 打回+批注 | 写原因；LLM 按批注调整参数后出新一轮（留空 = 只打回） |

   控制台快捷键：`1–9` 高亮 · `Enter` 通过 · `F` 微调 · `R` 重做 · `X` 打回 · `J/K` 切换审阅项。
4. **锁定后延续**：基准包锁定“角色”后，后续需求可以说“用这个角色……”，规划器会用 `@pack:角色` 作为参考图（参考编辑 / 图生视频）。

### 3.2 项目
- 一个创作目标一个项目。所有轮次、任务、基准包版本、对话都挂在项目下。
- 「项目与对话」页：列表含**已完成**项目，可随时翻看历史；点“设为当前”继续对话。
- 完成后点“标记完成”（或 `/project done`）：排队任务取消，历史永久保留。

### 3.3 命令（Telegram 与控制台对话框通用，不唤醒 LLM）

| 命令 | 作用 |
|---|---|
| `/status` | 项目、出图状态、排队数、内存、LLM 模式 |
| `/review` | 把当前项目待审阅的轮次重新推送到 Telegram |
| `/round 模板 [数量] [prompt]` | 不经 LLM 直接出一轮（模板：`flux2-t2i` `flux2-edit` `h3-t2v` `h3-i2v`） |
| `/redo` | 最近一轮待审阅的整轮重做 |
| `/pause` `/resume` | 暂停 / 恢复出图（正在跑的会跑完） |
| `/cancel` | 取消当前项目的排队任务，并中断本系统正在跑的任务 |
| `/mode [on_demand\|session\|always\|command_only]` | LLM 驻留模式（默认按需） |
| `/project` · `/projects` · `/project new 名称` · `/project use 名称` · `/project done` | 项目管理 |
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
uv run va submit ...                    # 阶段 1 的同步提交（daemon 运行时会因串行锁等待/拒绝）
```

## 4. 资源与内存策略（自动执行）

| 情况 | 系统行为 |
|---|---|
| 提交视频前 | **卸载 27B**（视频峰值约 55 GiB，共存会进内存压力警告），需要可用 ≥ 62 GiB |
| 视频结束 | 队列空闲则 `/free`；27B 以 TTL 5 min 重新加载 |
| 图片任务 | 不卸载 27B；保留图片模型（热态 11 s vs 冷启动 23 s） |
| 内存不足 | 依次：`/free`（仅队列空闲）→ 卸载 LLM → 等待最多 10 min → 超时不提交 |
| ComfyUI 有别人的任务 | 等队列空闲再提交，不抢占、不取消 |
| 视频生成中收到自由文本 | LLM 排队，生成结束后处理；命令照常即时响应 |

## 5. 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| 控制台顶栏“daemon 未运行” | `pgrep -fl "va daemon"` | 按 §2 启动；排队任务会自动继续 |
| “ComfyUI 离线” | 打开 http://127.0.0.1:8188 | 启动 FreeVideo-local 的 ComfyUI；aco_lw 不会自己启动它 |
| 任务卡在“等待内存” | 顶栏内存 / 压力；ComfyUI 队列 | 关掉占内存的应用，或 `/cancel`；超过 10 min 会标为超时、不提交 |
| 任务“超时” | `va status <ID>` 看 prompt_id | 生成可能仍在跑：`va recover` 对账收回；不要手动重提 |
| 任务“失败” | 控制台详情页的错误 / ComfyUI 终端日志 | 改参数后“重做”；失败的任务允许同参数重提 |
| Telegram 没反应 | `grep 忽略非白名单 data/daemon.log` | chat_id 不在白名单：发 `/whoami`，把数字加入 `config.toml` 的 `telegram_chat_ids` 后重启 |
| Telegram 发送失败 | `grep "Telegram 发送" data/daemon.log` | 网络或 token 问题；token 泄露时在 @BotFather `/revoke`，更新 `config.toml` 后重启 |
| LLM 回复很慢（> 40 s） | 控制台对话里 LLM 气泡上的耗时 | 正常范围 7–40 s（上下文越长越慢）；冷启动多约 15 s |
| LLM 理解错 | 对话历史 | 用按钮“微调 / 打回+批注”针对具体一轮说；或 `/round` 直接给 prompt |
| 另一个编排器在运行 | 报错 `另一个编排器进程正在运行` | daemon 运行时不要同时用 `va submit`；改用 `va round` 排队 |

## 6. 数据、备份与清理
- **备份**：`data/tasks.sqlite`、`data/jobs/`、`data/packs/`、`config.toml`。daemon 停止时复制最安全（SQLite 用 WAL，运行中复制需连同 `-wal` 文件）。
- **产物路径**：控制台任一产物右键另存；或 `data/jobs/<任务ID>/`。ComfyUI 侧原始文件在 `FreeVideo-local/ComfyUI/output/` 下（`aco_lw/<任务ID>` 前缀或 `FreeVideo/<日期>/`）。
- **清理**：已完成项目可以保留；要删除产物请先停 daemon，再删对应 `data/jobs/<ID>` 目录（数据库记录仍在，控制台会显示缺图）。

## 7. 安全约束（不要改）
- 只有编排器能碰 ComfyUI；LLM 与 Telegram 只产生数据或调用固定命令。
- 不取消、不释放别人的任务；`/free` 前二次确认队列为空。
- 不修改 ComfyUI 启动参数、全局 LM Studio / OpenClaw 配置；不下载模型、不装自定义节点。
- Telegram 只响应白名单；控制台只绑定 127.0.0.1。

## 8. 已知问题（2026-10-26 测试记录）
1. **参考图还不能通过对话给**：在批注里贴本机图片路径，规划器不会把它当参考图（文生图模板没有图片输入）。临时做法：先把参考图对应的任务“通过”并锁为角色，再提需求。→ 计划支持对话 / Telegram 直接附图。
2. **对上一轮的自由文本反馈会开成新需求**：不点“微调 / 打回”而直接打字时，会作为新需求处理，DAG 上不连到上一轮，上一轮仍留在待审阅。→ 计划：有待审阅轮次时，自由文本默认视为对最近一轮的批注。
3. 规划器偶尔误解词义（例如把“太小（年龄）”理解成“画面太小”）。批注写得越具体越稳。
