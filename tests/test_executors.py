"""E1：编排器只通过 Executor 接口工作；换一个执行端不需要改编排器。"""

from pathlib import Path

from video_agent.executors import Poll, Prepared


class FakeExecutor:
    """一个不是 ComfyUI 的执行端：第二次轮询完成，产出一个文本文件。"""

    name = "fake"

    def __init__(self, fail=False, lost=False):
        self.jobs, self.fail, self.lost, self.released = {}, fail, lost, 0

    def prepare(self, template, task):
        return Prepared(payload={"p": task["params"]}, artifacts={"job.json": "{}"}, note="ok")

    def submit(self, prepared, job_id):
        self.jobs[job_id] = 0

    def poll(self, job_id):
        if self.lost:
            return Poll("lost")
        self.jobs[job_id] += 1
        if self.jobs[job_id] < 2:
            return Poll("running")
        return Poll("failed" if self.fail else "done", result={"text": "hello"})

    def failure_detail(self, result):
        return "boom"

    def collect(self, job_id, result, dest: Path, metrics):
        (dest / "out.png").write_text(result["text"])
        metrics["exec_s"] = 1.0
        return [{"file": "out.png", "kind": "image"}]

    def cancel(self, job_id):
        pass

    def busy(self):
        return False

    def release(self):
        self.released += 1
        return True


def use_fake(orch, ex):
    orch.executors["fake"] = ex
    for t in orch.registry.templates.values():
        t.executor = "fake"
    return ex


def test_orchestrator_runs_any_executor(make_orch, fake):
    o = make_orch(fake)
    ex = use_fake(o, FakeExecutor())
    t = o.execute(o.create("h3-t2v", {"seed": 1})["id"])
    assert t["state"] == "review" and t["outputs"][0]["file"] == "out.png"
    assert (o.store.task_dir(t["id"]) / "job.json").exists()
    assert ex.released == 1  # 视频模板 release_after=free
    assert fake.submits == 0  # 没碰 ComfyUI


def test_executor_failure_and_lost(make_orch, fake):
    o = make_orch(fake)
    use_fake(o, FakeExecutor(fail=True))
    t = o.execute(o.create("flux2-t2i", {"seed": 1})["id"])
    assert t["state"] == "failed" and t["error"] == "boom"
    use_fake(o, FakeExecutor(lost=True))
    t = o.execute(o.create("flux2-t2i", {"seed": 2})["id"])
    assert t["state"] == "failed" and t["error"] == "lost"  # 连续几次找不到才认定丢失
