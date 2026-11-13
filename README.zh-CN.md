# aco_lw · 轻量本地视频 Agent

English: [README.md](README.md)

确定性编排器驱动本机 ComfyUI（FLUX.2 出图、FreeVideo / MiniMax H3 出视频）。本地 LLM（LM Studio）是可选的规划器，按需唤醒。候选在 Web 控制台或 Telegram 上审阅；所有内容按项目组织，每个项目有多个会话、共享的项目记忆和版本化的基准包。

- 计划与范围：[`PLAN.zh-CN.md`](PLAN.zh-CN.md) · 设计：[`docs/design.zh-CN.html`](docs/design.zh-CN.html) · 实测：[`docs/baseline.zh-CN.md`](docs/baseline.zh-CN.md)
- 运维：[`docs/runbook.zh-CN.md`](docs/runbook.zh-CN.md) · 进度：[`docs/progress.zh-CN.html`](docs/progress.zh-CN.html) · 架构：[`docs/architecture.zh-CN.html`](docs/architecture.zh-CN.html)

## 快速开始

```sh
uv sync
cp config.example.toml config.toml        # 可选：Telegram token、语言（language = "zh"）、端口
uv run va daemon                          # Worker + 规划器 + Telegram + 控制台
open http://127.0.0.1:8790                # 控制台（默认英文，顶栏 EN / 中文 切换）
```

当前要求：Apple Silicon Mac、运行中的 ComfyUI（含模板所需模型），以及可选的 LM Studio。一行命令安装 + 配置向导在阶段 5 提供。

## 结构

| 层 | 作用 |
|---|---|
| 编排器 | 唯一有副作用的组件：校验、内存门控、提交、轮询、收集、释放、崩溃安全的恢复 |
| Studio | 项目 → 轮次 → 任务；审阅决定（通过 / 重做 / 微调 / 打回 / 更改审阅结果）；基准包 |
| 规划器（可选） | 把自然语言翻译成经校验的参数；会话、项目记忆、对提示缓存友好的上下文 |
| 渠道 | Web 控制台与 Telegram 共用同一套操作 |

## 命令行

```sh
uv run va templates                                  # 模板、档位（✓ 已实测）、参数
uv run va round flux2-t2i -n 4 -p prompt="..."       # 排队一轮（由 daemon 执行）
uv run va status [任务ID] · va project list|new|use|done · va pack · va mode
uv run va recover · va release
uv run pytest · uv run ruff check
```

运行数据（SQLite、每任务 manifest 与产物、基准包、上传的参考图）在 `data/`，不进 git。
