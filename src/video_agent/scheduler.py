"""确定性编排器：所有执行端（ComfyUI 等）的副作用只经这里、经 Executor 接口发生。

一个任务：草稿 → 已校验 → 等待内存 → 已提交 → 生成中 → 待审阅 → 完成 / 打回 / 失败 / 超时。
prompt_id 先落库再提交；重启后按 prompt_id 去 /history 与 /queue 对账，永不自动重提。
"""

from __future__ import annotations

import contextlib
import fcntl
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from .comfy import Comfy
from .config import Settings
from .executors import Executor, Poll, get_executors
from .memory import Sampler
from .registry import Registry, Template
from .render import params_hash, resolve
from .resources import ResourceManager
from .store import Store

LOST_POLLS = 5  # 连续几次在队列与历史里都找不到，才认定任务丢失


class SchedulerBusy(RuntimeError):
    pass


class DuplicateTask(RuntimeError):
    def __init__(self, existing: dict):
        super().__init__(f"相同参数的任务已存在：{existing['id']}（{existing['state']}）")
        self.existing = existing


class Orchestrator:
    def __init__(
        self,
        cfg: Settings,
        comfy: Comfy | None = None,
        store: Store | None = None,
        registry: Registry | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.executors: dict[str, Executor] = get_executors(cfg, comfy)
        self.comfy = self.executors["comfy"].comfy  # 兼容：取消 / 状态等仍需直接访问的地方
        self.store = store or Store(cfg.db_path, cfg.jobs_dir)
        self.registry = registry or Registry.from_settings(cfg)
        self.log = log
        self.sleep = sleep
        self.on_llm_restored: Callable[[], None] | None = None  # 由 daemon 注入：恢复后预热会话缓存
        self.resources = ResourceManager(cfg, self.registry, self.store, log, sleep)
        self.resources.executors = self.executors

    def executor_for(self, t: Template) -> Executor:
        if t.executor not in self.executors:
            raise ValueError(f"{t.name}: 未知执行端 {t.executor}")
        return self.executors[t.executor]

    # ---- 串行锁：同一时刻只有一个编排器进程在提交/对账 ----
    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with self.cfg.lock_path.open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SchedulerBusy("另一个编排器进程正在运行；未提交新任务") from None
            yield

    # ---- 创建 ----
    def create(
        self,
        template: str,
        params: dict,
        tier: str | None = None,
        title: str | None = None,
        parent_id: str | None = None,
        allow_unverified: bool = False,
        allow_duplicate: bool = False,
        project_id: str | None = None,
        round_id: str | None = None,
        idx: int | None = None,
        priority: int | None = None,
    ) -> dict:
        t = self.registry.template(template)
        params = dict(params)
        if parent_id:
            parent = self.store.get(parent_id)
            if parent is None:
                raise KeyError(f"父任务 {parent_id} 不存在")
            self._inherit_image(t, params, parent)
        for name, slot in t.slots.items():
            if slot.type == "image" and isinstance(params.get(name), str):
                params[name] = self.resolve_ref(params[name], project_id)
        tier, resolved = resolve(t, params, tier, allow_unverified)
        h = params_hash(t, resolved)
        dup = self.store.find_duplicate(h)
        if dup and not allow_duplicate:
            raise DuplicateTask(dup)
        task = self.store.create(
            template=t.name,
            template_sha=t.sha,
            kind=t.kind,
            tier=tier,
            params=resolved,
            params_hash=h,
            title=title,
            parent_id=parent_id,
            project_id=project_id,
            round_id=round_id,
            idx=idx,
            priority=priority,
        )
        self.store.touch_project(project_id)
        return task

    def resolve_ref(self, value: str, project_id: str | None) -> str:
        """图片引用：`@pack:角色` → 项目基准包最新版本里的文件；`@task:ID` → 该任务第一张图。"""
        if value.startswith("@pack:"):
            role = value[len("@pack:") :]
            pack = self.store.pack(project_id) if project_id else None
            if not pack or role not in pack["assets"]:
                raise ValueError(f"基准包里没有 {role}")
            return pack["assets"][role]["file"]
        if value.startswith("@upload:"):
            up = self.store.get_upload(value[len("@upload:") :])
            if up is None:
                raise ValueError(f"找不到上传的图片 {value}")
            return up["path"]
        if value.startswith("@task:"):
            src = self.store.get(value[len("@task:") :])
            if src is None:
                raise ValueError(f"找不到任务 {value[len('@task:') :]}")
            img = next((o for o in src["outputs"] if o["kind"] == "image"), None)
            if img is not None:
                return str(self.store.task_dir(src["id"]) / img["file"])
            vid = next((o for o in src["outputs"] if o["kind"] == "video"), None)
            if vid is not None:
                return str(self._video_frame(self.store.task_dir(src["id"]) / vid["file"]))
            raise ValueError(f"任务 {src['id']} 还没有可用作参考的图片或视频产物")
        return value

    @staticmethod
    def _video_frame(video: Path) -> Path:
        """视频任务用作参考图时，取中间一帧（缓存在任务目录）。"""
        frame = video.with_name(video.stem + "_mid.png")
        if frame.exists():
            return frame
        if shutil.which("ffmpeg") is None:
            raise ValueError("需要 ffmpeg 才能从视频取参考帧（brew install ffmpeg）")
        dur = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        mid = float(dur) / 2 if dur else 0.0
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{mid:.2f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                str(frame),
            ],
            check=True,
        )
        return frame

    def _inherit_image(self, t: Template, params: dict, parent: dict) -> None:
        """子任务未指定图片时，用父任务的第一张图片产物（例如定妆图 → 图生视频）。"""
        slot = next((n for n, s in t.slots.items() if s.type == "image"), None)
        if slot is None or slot in params:
            return
        img = next((o for o in parent["outputs"] if o["kind"] == "image"), None)
        if img is None:
            raise ValueError(f"父任务 {parent['id']} 没有图片产物可用作 {slot}")
        params[slot] = str(self.store.task_dir(parent["id"]) / img["file"])

    # ---- 执行 ----
    def execute(self, task_id: str) -> dict:
        with self.lock():
            return self._execute(task_id)

    def _execute(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        t = self.registry.template(task["template"])
        ex = self.executor_for(t)
        tdir = self.store.task_dir(task_id)

        try:
            prepared = ex.prepare(t, task)
        except Exception as e:  # noqa: BLE001 — 校验失败的任何原因都记录后终止
            return self.store.transition(task_id, "failed", f"校验失败：{e}", error=str(e))
        for name, text in prepared.artifacts.items():
            (tdir / name).write_text(text)
        self.store.transition(task_id, "validated", prepared.note or None)
        self.store.update(task_id, metrics={"uploads": prepared.uploads})

        est = self.resources.estimator.estimate(t, task["tier"])
        self.store.transition(
            task_id,
            "waiting_memory",
            f"需要 {est.peak_gib + self.cfg.memory_margin_gib:.0f} GiB · 预计 {est.exec_s:.0f} s",
        )
        gate = self.resources.prepare_render(t, task["tier"], ex, task_id)
        metrics = {
            **self.store.get(task_id)["metrics"],
            "gate": {
                "required_gib": gate.required_gib,
                "actions": gate.actions,
                "waited_s": gate.waited_s,
                "at_submit": gate.last,
            },
        }
        self.store.update(task_id, metrics=metrics)
        try:
            if not gate.ok:
                return self.store.transition(
                    task_id, "timeout", "内存门控等待超时，未提交", error="memory gate timeout"
                )
            job_id = str(uuid.uuid4())
            self.store.update(task_id, prompt_id=job_id)  # 先落库再提交
            try:
                ex.submit(prepared, job_id)
            except Exception as e:  # noqa: BLE001
                return self.store.transition(task_id, "failed", f"提交失败：{e}", error=str(e))
            self.store.transition(task_id, "submitted", f"job {job_id}")
            return self._monitor(task_id)
        finally:
            self._after(t, ex, gate.unloaded_llm)

    def _monitor(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        ex = self.executor_for(self.registry.template(task["template"]))
        job_id = task["prompt_id"]
        start = time.monotonic()
        lost = 0
        # 观察时限随预计耗时放大（未实测的大档位可能要几个小时），至少保留配置值
        est = self.resources.estimator.estimate(
            self.registry.template(task["template"]), task["tier"]
        )
        limit = max(self.cfg.task_timeout_s, est.exec_s * 3)
        poll: Poll | None = None
        with Sampler() as sampler:
            while True:
                poll = ex.poll(job_id)
                if poll.state in ("done", "failed"):
                    break
                lost = lost + 1 if poll.state == "lost" else 0
                if lost >= LOST_POLLS:
                    break
                task = self.store.get(task_id)
                if task["state"] == "submitted" and poll.state == "running":
                    self.store.transition(task_id, "running")
                if time.monotonic() - start > limit:
                    poll = None
                    break
                self.sleep(self.cfg.poll_interval_s)
        metrics = {
            **self.store.get(task_id)["metrics"],
            "memory": sampler.summary(),
            "observed_s": round(time.monotonic() - start, 1),
        }
        self.store.update(task_id, metrics=metrics)
        if poll is None:
            return self.store.transition(
                task_id,
                "timeout",
                "观察超时；任务未取消，可用 recover 继续对账",
                error="monitor timeout",
            )
        if poll.state == "lost":
            return self.store.transition(
                task_id, "failed", f"{ex.name} 中找不到该任务；不自动重提", error="lost"
            )
        return self._finish(task_id, poll, ex)

    def _finish(self, task_id: str, poll: Poll, ex: Executor) -> dict:
        task = self.store.get(task_id)
        metrics, job_id = dict(task["metrics"]), task["prompt_id"]
        if poll.state == "failed":
            detail = ex.failure_detail(poll.result)
            ex.collect(job_id, poll.result, self.store.task_dir(task_id), metrics)
            self.store.update(task_id, metrics=metrics)
            return self.store.transition(
                task_id, "failed", f"{ex.name} 执行失败：{detail}", error=detail[:2000]
            )
        outputs = ex.collect(job_id, poll.result, self.store.task_dir(task_id), metrics)
        self.store.update(task_id, metrics=metrics)
        if not outputs:  # 报告完成却没有产物：按失败处理，别进待审阅
            msg = f"{ex.name} reported success but produced no output"
            return self.store.transition(task_id, "failed", msg, error=msg)
        return self.store.transition(
            task_id, "review", f"收集 {len(outputs)} 个产物", outputs=outputs
        )

    def _after(self, t: Template, ex: Executor, unloaded_llm: bool) -> None:
        if self.registry.model_of(t).resource == "cpu":
            return  # 没占内存闸门，也就没有要还的
        released = False
        if t.release_after == "free":
            released = ex.release()
            self.log(f"{ex.name} {'released' if released else 'busy; release skipped'}")
        self.resources.render_done(t, released, unloaded_llm, self.on_llm_restored)

    # ---- 恢复 ----
    def recover(self) -> list[tuple[str, str]]:
        """进程重启后对账；只观察与收集，不重新提交。"""
        results = []
        with self.lock():
            # 排队中（draft）的任务从未发出过：原样留在队列里，重启后照常执行
            stuck = self.store.by_state(
                "validated", "waiting_memory", "submitted", "running", "timeout"
            )
            for task in stuck:
                results.append((task["id"], self._reconcile(task)))
        return results

    def reconcile_timeouts(self) -> list[tuple[str, str]]:
        """把“观察超时”但执行端已跑完的任务收回来（不阻塞：还在跑的留到下次）。"""
        out = []
        for task in self.store.by_state("timeout"):
            if not task["prompt_id"]:
                continue
            ex = self.executor_for(self.registry.template(task["template"]))
            poll = ex.poll(task["prompt_id"])
            if poll.state in ("done", "failed"):
                out.append((task["id"], self._finish(task["id"], poll, ex)["state"]))
        return out

    def _reconcile(self, task: dict) -> str:
        tid, pid, state = task["id"], task["prompt_id"], task["state"]
        if state == "validated":
            self.store.transition(tid, "failed", "中断于提交前；可重新提交", error="interrupted")
            return "failed（提交前中断）"
        if not pid:
            if state == "waiting_memory":
                self.store.transition(tid, "failed", "中断于内存门控；未提交", error="interrupted")
                return "failed（未提交）"
            return f"跳过（{state} 无 prompt_id）"
        ex = self.executor_for(self.registry.template(task["template"]))
        poll = ex.poll(pid)
        if poll.state in ("done", "failed"):
            if state == "waiting_memory":
                self.store.transition(tid, "submitted", "恢复：执行端已有结果")
            return self._finish(tid, poll, ex)["state"]
        if poll.state in ("queued", "running"):
            if state == "waiting_memory":
                self.store.transition(tid, "submitted", "恢复：在执行端队列中找到")
            if state == "timeout":
                return "仍在运行（保持 timeout，稍后再 recover）"
            return self._monitor(tid)["state"]
        if state == "timeout":
            return f"timeout（{ex.name} 中找不到）"
        self.store.transition(tid, "failed", f"{ex.name} 中找不到该任务；不自动重提", error="lost")
        return "failed（找不到）"

    # ---- 审阅与释放 ----
    def review(self, task_id: str, accept: bool, note: str | None = None) -> dict:
        return self.store.transition(task_id, "done" if accept else "rejected", note)

    def release(self) -> None:
        """释放所有空闲执行端的模型；有执行端忙则抛错（不动别人的任务）。"""
        from .comfy import ComfyBusy

        busy = [name for name, ex in self.executors.items() if not ex.release()]
        if busy:
            raise ComfyBusy(f"队列非空，拒绝释放：{', '.join(busy)}")
