# aco_lw · 轻量本地视频 Agent

确定性编排器驱动本机 ComfyUI（FLUX.2 出图、FreeVideo / MiniMax H3 出视频），LLM 可选、按需唤醒。
计划：`PLAN.md` · 设计：`docs/design.html` · 基线实测：`docs/baseline.md` · 进度：`docs/progress.html`

```sh
uv sync
uv run va templates                                   # 模板、档位（✓ 已实测）、参数
uv run va submit flux2-t2i -p seed=1 --title 定妆图     # 提交并等待到「待审阅」
uv run va submit h3-i2v --parent <任务ID>               # 子任务：继承父任务图片 → 图生视频
uv run va run tasks.yaml                              # 批量（parent: $prev 链接上一项）
uv run va status [任务ID] · va review <ID> accept|reject · va recover · va release
uv run va serve                                       # Web 进度台 http://127.0.0.1:8790
uv run pytest
```

运行数据（SQLite、每任务目录 `manifest.json` + 产物）在 `data/`，不进 git。

## 阶段 2：常驻服务、审阅、Telegram

```sh
cp config.example.toml config.toml   # 填 telegram_token / telegram_chat_ids（可先发 /whoami 查 chat_id）
uv run va daemon                     # Worker + 规划器 + Telegram + 控制台 http://127.0.0.1:8790
```

控制台：`#flow` 创作 DAG · `#review` 审阅（通过 / 重做 / 微调 / 打回+批注，快捷键见页面）· `#projects` 项目与对话（含已完成项目）· `#gallery` 产物。
命令行：`va round <模板> -n 4` · `va project list|new|use|done` · `va pack` · `va mode`。
