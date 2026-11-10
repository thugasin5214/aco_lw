# video-agent

本地视频 Agent。计划与进度见 `PLAN.md`，设计依据见 `docs/design.html`。

## 硬约束
- 编排器是中转：agent（LLM）只提出 tool call，编排器（`src/video_agent/`）执行并择机交回结果（LLM 被卸载时结果暂存）。所有 ComfyUI / FreeVideo 副作用只经编排器的生成工具；通用工具（读网页、执行命令、读写文件、skill、agent 记忆、MCP 服务，见 `toolbox.py` / `mcp_hub.py`）也由编排器执行——需审批的动作（命令、非只读 MCP、委托外部 agent、未实测视频档位）默认每条经用户批准；用户可点“始终允许”记规则，或 `/permissions bypass` 全部跳过（`policies.yaml` 的 `run_command.auto_allow` 也可放行单条命令前缀）。任何模式下都拒绝直连 :8188、读 FreeVideo token、结束进程、加载/卸载 LLM；装节点 / 下模型在 bypass 下仍每次问（用户 2026-11-07 决定）。
- 例外：`/fv` 与 `delegate_freevideo`（每次经用户批准）把整个任务交给 FreeVideo agent——Claude Code + FreeVideo 自己的 MCP，由它直接在 FreeVideo 里生成（不进 aco 的任务 / 轮次；FreeVideo 队列仍被 aco 的资源闸门看见）。MCP 配置取自 FreeVideo 的 `/freevideo/agent/connect`、只写进本次运行的 `--mcp-config`，不改全局 Claude Code 配置；在线只由 `freevideo_agent_online` 放行。
- 不取消、不释放非本系统提交的任务/模型；队列非空拒绝 `/free`。
- 不改 ComfyUI 启动参数、全局 OpenClaw / LM Studio 配置。
- agent 可以探索 ComfyUI（模型、节点、workflow）、搭建 / 修改 workflow、按最低档试跑（都经编排器）；**下载模型、安装自定义节点必须先经用户批准**（用户 2026-10-27 决定）。
- FreeVideo agent API 的 token（`FreeVideo-local/agent/token`）只在进程内读取，不打印、不写进别的文件。
- 提交真实生成任务（尤其视频：FreeVideo 按可用内存自适应占用，一次以分钟计）前先征得用户确认。
- 数字标注“实测 / 估算”。运行数据全部放 `data/`（gitignored）。

## 环境
- ComfyUI: http://127.0.0.1:8188，实际是 `~/Documents/ChatGPT/jev/FreeVideo-local/ComfyUI`（视频 = FreeVideo / MiniMax H3）；`~/workplace/localvideo` 是旧环境，不用
- LM Studio: http://127.0.0.1:1234，CLI `lms`
- 源 API 模板：`~/Documents/ChatGPT/jev/workflows/标准实验室/*-API.json`
- 命令：`uv run pytest`，`uv run ruff check`，`uv run va ...`
- Web 进度台默认端口 8790（8765 被用户另一个 server 使用，勿占用）
