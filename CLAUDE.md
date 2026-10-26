# video-agent

本地视频 Agent。计划与进度见 `PLAN.md`，设计依据见 `docs/design.html`。

## 硬约束
- 所有 ComfyUI 副作用只在编排器（`src/video_agent/`）里发生；LLM / Telegram 不直接调 ComfyUI 或 shell。
- 不取消、不释放非本系统提交的任务/模型；队列非空拒绝 `/free`。
- 不改 ComfyUI 启动参数、全局 OpenClaw / LM Studio 配置；不下载模型、不装自定义节点。
- 提交真实生成任务（尤其视频：FreeVideo 按可用内存自适应占用，一次以分钟计）前先征得用户确认。
- 数字标注“实测 / 估算”。运行数据全部放 `data/`（gitignored）。

## 环境
- ComfyUI: http://127.0.0.1:8188，实际是 `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI`（视频 = FreeVideo / MiniMax H3）；`~/workplace/localvideo` 是旧环境，不用
- LM Studio: http://127.0.0.1:1234，CLI `lms`
- 源 API 模板：`~/Documents/ChatGPT/jev/workflows/标准实验室/*-API.json`
- 命令：`uv run pytest`，`uv run ruff check`，`uv run va ...`
