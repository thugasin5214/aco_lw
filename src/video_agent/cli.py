"""va：编排器的命令行入口。Telegram / LLM 以后也只调这几条命令背后的同一套函数。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml

from . import config
from .render import ParamError
from .scheduler import DuplicateTask, Orchestrator, SchedulerBusy
from .store import PROJECT_ZH, STATE_ZH
from .studio import Studio, StudioError

app = typer.Typer(no_args_is_help=True, add_completion=False, help="轻量本地视频 Agent 编排器")


def _orch() -> Orchestrator:
    return Orchestrator(config.load())


def _studio():
    cfg = config.load()
    return Studio(Orchestrator(cfg), cfg.round_size)


def _parse_params(items: list[str]) -> dict:
    out = {}
    for item in items:
        k, _, v = item.partition("=")
        if not _:
            raise typer.BadParameter(f"参数格式应为 key=value：{item}")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


def _submit_one(o: Orchestrator, spec: dict, allow_unverified: bool, allow_duplicate: bool) -> dict:
    try:
        task = o.create(
            spec["template"],
            spec.get("params", {}),
            spec.get("tier"),
            spec.get("title"),
            spec.get("parent"),
            allow_unverified,
            allow_duplicate,
        )
    except (ParamError, KeyError, ValueError) as e:
        _fail(f"参数无效：{e}")
    except DuplicateTask as e:
        _fail(f"{e}；如确需重跑，加 --allow-duplicate")
    typer.echo(f"任务 {task['id']} 已创建（{task['template']} / {task['tier']}）")
    try:
        task = o.execute(task["id"])
    except SchedulerBusy as e:
        _fail(f"{e}。任务 {task['id']} 保持草稿，可稍后 `va recover`。")
    color = "green" if task["state"] == "review" else "red"
    typer.secho(f"{task['id']}: {STATE_ZH[task['state']]}", fg=color)
    for out in task["outputs"]:
        typer.echo(f"  {o.store.task_dir(task['id']) / out['file']}")
    if task["error"]:
        typer.echo(f"  错误：{task['error']}")
    return task


@app.command()
def templates() -> None:
    """列出模板、档位（✓ 已实测）与可调参数。"""
    o = _orch()
    for t in o.registry.templates.values():
        tiers = ", ".join(f"{n}{'✓' if tr.verified else ''}" for n, tr in t.tiers.items())
        typer.echo(f"{t.name:<12} {t.kind:<5} {t.title}")
        typer.echo(f"    档位：{tiers}（默认 {t.default_tier}）")
        typer.echo(f"    参数：{', '.join(t.slots)}")


@app.command()
def submit(
    template: str,
    param: Annotated[list[str], typer.Option("-p", "--param", help="key=value，可多次")] = [],  # noqa: B006
    prompt_file: Annotated[Path | None, typer.Option(help="从文件读取 prompt")] = None,
    tier: Annotated[str | None, typer.Option(help="档位，默认取模板默认档")] = None,
    title: Annotated[str | None, typer.Option()] = None,
    parent: Annotated[str | None, typer.Option(help="父任务 ID；未给图片时继承其图片产物")] = None,
    allow_unverified: bool = False,
    allow_duplicate: bool = False,
) -> None:
    """校验并提交一个任务，阻塞直到待审阅 / 失败 / 超时。"""
    params = _parse_params(param)
    if prompt_file:
        params["prompt"] = prompt_file.read_text().strip()
    spec = {"template": template, "params": params, "tier": tier, "title": title, "parent": parent}
    _submit_one(_orch(), spec, allow_unverified, allow_duplicate)


@app.command()
def run(
    taskfile: Path,
    allow_unverified: bool = False,
    allow_duplicate: bool = False,
) -> None:
    """批量入口：YAML 列表，每项 {template, params, tier, title, parent}；按顺序串行执行。

    parent 可写 "$prev" 表示上一项生成的任务。
    """
    o = _orch()
    prev = None
    for spec in yaml.safe_load(taskfile.read_text()):
        if spec.get("parent") == "$prev":
            spec["parent"] = prev
        prev = _submit_one(o, spec, allow_unverified, allow_duplicate)["id"]


@app.command()
def status(task_id: Annotated[str | None, typer.Argument()] = None, limit: int = 20) -> None:
    """列出最近任务，或显示单个任务的 manifest。"""
    o = _orch()
    if task_id:
        task = o.store.get(task_id)
        if task is None:
            _fail(f"找不到任务 {task_id}")
        task["events"] = o.store.events(task_id)
        typer.echo(json.dumps(task, ensure_ascii=False, indent=2))
        return
    for t in o.store.list(limit):
        typer.echo(
            f"{t['id']}  {STATE_ZH[t['state']]:<4}  {t['template']:<12} {t['tier']:<8} "
            f"{t['title'] or ''}"
        )


@app.command()
def review(
    task_id: str,
    verdict: Annotated[str, typer.Argument(help="accept / reject")],
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """审阅单个任务：accept → 完成，reject → 打回（属于某轮时按整轮处理）。"""
    if verdict not in ("accept", "reject"):
        _fail("verdict 只能是 accept 或 reject")
    try:
        _studio().approve_task(task_id, verdict == "accept", note, origin="cli")
    except StudioError as e:
        _fail(str(e))
    typer.echo(f"{task_id}: {STATE_ZH[_orch().store.get(task_id)['state']]}")


@app.command()
def recover() -> None:
    """重启后对账：按 prompt_id 查 /history 与 /queue，只收集不重提。"""
    try:
        results = _orch().recover()
    except SchedulerBusy as e:
        _fail(str(e))
    if not results:
        typer.echo("没有需要对账的任务")
    for tid, outcome in results:
        typer.echo(f"{tid}: {outcome}")


@app.command()
def release() -> None:
    """队列空闲时请求 ComfyUI 释放模型；队列非空则拒绝。"""
    try:
        _orch().release()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))
    typer.echo("已请求释放")


@app.command()
def plan() -> None:
    """自然语言需求请在 Web 控制台“项目与对话”或 Telegram 里直接说（需 `va daemon`）。"""
    _fail("请在控制台对话框或 Telegram 里描述需求；命令行请用 round / submit。")


@app.command()
def serve(host: str = "127.0.0.1", port: Annotated[int | None, typer.Option()] = None) -> None:
    """只启动 Web 控制台（不出图；排队任务需要 `va daemon` 才会执行）。"""
    import uvicorn

    from .web import create_app

    cfg = config.load()
    uvicorn.run(create_app(cfg), host=host, port=port or cfg.web_port, log_level="warning")


@app.command()
def daemon(host: str = "127.0.0.1", port: Annotated[int | None, typer.Option()] = None) -> None:
    """常驻服务：Worker 串行出图 + 规划器队列 + Telegram 监听 + Web 控制台。"""
    import logging

    import uvicorn

    from .daemon import Daemon
    from .web import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config.load()
    d = Daemon(cfg)
    d.start()
    typer.echo(f"Console: http://{host}:{port or cfg.web_port}")
    try:
        uvicorn.run(
            create_app(cfg, d.actions), host=host, port=port or cfg.web_port, log_level="warning"
        )
    finally:
        d.shutdown()


@app.command("round")
def round_(
    template: str,
    param: Annotated[list[str], typer.Option("-p", "--param", help="key=value，可多次")] = [],  # noqa: B006
    n: Annotated[int | None, typer.Option("-n", help="候选数，默认取 round_size（视频 1）")] = None,
    tier: Annotated[str | None, typer.Option()] = None,
    title: Annotated[str | None, typer.Option()] = None,
    allow_unverified: bool = False,
) -> None:
    """开一轮候选（只排队，由 `va daemon` 的 Worker 执行）。"""
    try:
        rnd = _studio().start_round(
            template,
            _parse_params(param),
            n,
            tier,
            title,
            origin="cli",
            allow_unverified=allow_unverified,
        )
    except (ParamError, KeyError, ValueError) as e:
        _fail(f"参数无效：{e}")
    typer.echo(f"轮 {rnd['id']}：{rnd['n']} 个候选已排队（{rnd['template']} / {rnd['tier']}）")


@app.command()
def project(
    action: Annotated[str, typer.Argument(help="list / new / use / done")] = "list",
    name: Annotated[str | None, typer.Argument()] = None,
) -> None:
    """项目：list 全部（含已完成）、new 新建并切换、use 切换、done 标记完成。"""
    s = _studio()
    try:
        if action == "new" and name:
            p = s.new_project(name)
        elif action == "use" and name:
            p = s.use_project(name)
        elif action == "done":
            p = s.finish_project(s.use_project(name)["id"] if name else None)
        elif action == "list":
            cur = s.current_project()["id"]
            for p in s.store.list_projects():
                mark = "*" if p["id"] == cur else " "
                typer.echo(f"{mark} {PROJECT_ZH[p['state']]:<4} {p['name']}  ({p['id']})")
            return
        else:
            _fail("用法：va project [list | new 名称 | use 名称 | done [名称]]")
    except StudioError as e:
        _fail(str(e))
    typer.echo(f"{p['name']}：{PROJECT_ZH[p['state']]}")


@app.command()
def pack() -> None:
    """当前项目的基准包版本。"""
    s = _studio()
    hist = s.store.pack_history(s.current_project()["id"])
    if not hist:
        typer.echo("当前项目还没有基准包")
    for v in hist:
        roles = "，".join(f"{r} ← {a['task_id']}" for r, a in v["assets"].items())
        typer.echo(f"v{v['version']}  {roles}  {v['note'] or ''}")


@app.command()
def mode(value: Annotated[str | None, typer.Argument()] = None) -> None:
    """LLM 驻留模式：on_demand / session / always / command_only。"""
    from .planner import MODES, Planner

    cfg = config.load()
    o = _orch()
    pl = Planner(cfg, o.store, o.registry)
    if value:
        try:
            pl.set_mode(value)
        except ValueError as e:
            _fail(str(e))
    typer.echo(f"{pl.mode}：{MODES[pl.mode]}")


if __name__ == "__main__":
    app()
