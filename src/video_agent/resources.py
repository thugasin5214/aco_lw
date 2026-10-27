"""动态资源调度（R1 + Q1，替代 memgate 的固定阈值）。

原则（与 vhotseat 一致）：
- 运行中的工作绝不打断：LLM 正在写的回答要等它写完；渲染中不抢。
- 在“加载 / 提交前”把关：LLM 推理前问 `llm_admit`；渲染提交前走 `prepare_render`。
- 少交接：同一模型族的作业连着跑，切换代价计入排序。

估计来自历史 metrics（按模板 + 档位），没有历史时回退到模型注册表与档位成本比例。
"""

from __future__ import annotations

import contextlib
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import llm
from .memory import snapshot
from .registry import ModelInfo, Template

PRIORITY = {"interactive": 2, "agent": 1, "background": 0}
AGING_PER_MIN = 20  # 每等待 1 分钟加的分（防饿死）
AGING_CAP = 1500  # 50 min 后后台任务可超过新的 agent 任务，但永远不超过你的交互请求
ANSWER_WAIT_S = 600  # 等 LLM 写完当前回答的上限（不打断回答）
SAME_FAMILY_BONUS = 300  # 与当前常驻模型族相同 → 省一次切换
LLM_LOAD_S = 15.4  # G0 实测
DEFAULT_EXEC_S = {"image": 20.0, "video": 330.0}


def tier_cost(values: dict) -> float:
    return values.get("width", 1) * values.get("height", 1) * values.get("seconds", 1)


@dataclass
class Estimate:
    exec_s: float
    peak_gib: float
    family: str
    source: str


class Estimator:
    """从已完成任务的 metrics 学习（中位数，抗个别异常），按档位成本比例外推未实测的档位。"""

    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    def _history(self, template: str) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT tier, metrics FROM tasks WHERE template=? AND state IN ('review','done','rejected')"
            " ORDER BY created DESC LIMIT 60",
            (template,),
        ).fetchall()
        import json

        out = []
        for r in rows:
            m = json.loads(r["metrics"])
            if m.get("exec_s"):
                peak = (m.get("freevideo") or {}).get("ram_peak_bytes")
                out.append(
                    {
                        "tier": r["tier"],
                        "exec_s": m["exec_s"],
                        "peak_gib": peak / 2**30 if peak else None,
                    }
                )
        return out

    def estimate(self, template: Template, tier: str) -> Estimate:
        model: ModelInfo = self.registry.model_of(template)
        hist = self._history(template.name)
        cost = tier_cost(template.tiers[tier].values()) if tier in template.tiers else 1
        same = [h for h in hist if h["tier"] == tier]
        peaks = [h["peak_gib"] for h in hist if h["peak_gib"]]
        peak = max(peaks) if peaks else model.peak_gib
        if same:
            return Estimate(
                statistics.median(h["exec_s"] for h in same),
                peak,
                template.model,
                f"history n={len(same)}",
            )
        # 外推：同模板其他档位按成本比例换算
        for h in hist:
            if h["tier"] in template.tiers:
                base = tier_cost(template.tiers[h["tier"]].values())
                return Estimate(
                    h["exec_s"] * cost / base, peak, template.model, f"scaled from {h['tier']}"
                )
        return Estimate(DEFAULT_EXEC_S[template.kind], peak, template.model, "default")


class LLMWorkload:
    """本地 LLM 作为一个工作负载：是否加载、正在进行的推理数（推理中绝不卸载）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.in_flight = 0
        self.cond = threading.Condition()

    def loaded(self) -> bool:
        return llm.loaded(self.cfg.llm_model) is not None

    @contextlib.contextmanager
    def inference(self) -> Iterator[None]:
        with self.cond:
            self.in_flight += 1
        try:
            yield
        finally:
            with self.cond:
                self.in_flight -= 1
                self.cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: self.in_flight == 0, timeout)

    def unload(self) -> None:
        llm.unload(self.cfg.llm_model)

    def load(self, ttl: int | None) -> None:
        llm.load(self.cfg.llm_model, self.cfg.llm_context, ttl)


@dataclass
class RenderGate:
    ok: bool
    required_gib: float
    actions: list[str] = field(default_factory=list)
    unloaded_llm: bool = False
    last: dict = field(default_factory=dict)
    waited_s: float = 0.0


class ResourceManager:
    def __init__(
        self,
        cfg,
        registry,
        store,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg, self.registry, self.store = cfg, registry, store
        self.log, self.sleep = log, sleep
        self.llm = LLMWorkload(cfg)
        self.estimator = Estimator(store, registry)
        self.render_active: dict | None = None  # {"task_id", "family", "kind", "exclusive"}
        self.resident_family: str | None = None
        self.listeners: list[Callable[[], None]] = []  # 渲染结束 → 唤醒被暂存的 LLM 作业
        self._lock = threading.RLock()

    @staticmethod
    def snap() -> dict:
        return snapshot()  # 运行时查找，测试可替换模块级 snapshot

    # ---- LLM 把关 ----
    def llm_admit(self) -> tuple[bool, str]:
        """LLM 现在能不能推理？不能时调用方应暂存（不阻塞），渲染结束后会被唤醒。"""
        with self._lock:
            ra = self.render_active
            if ra and ra["exclusive"]:
                return False, f"{ra['kind']} render running ({ra['family']}) — needs the memory"
        if self.snap()["pressure"] >= 4:
            return False, "memory pressure critical"
        return True, ""

    # ---- 渲染把关（替代 memgate.ensure） ----
    def prepare_render(self, template: Template, tier: str, executor, task_id: str) -> RenderGate:
        model = self.registry.model_of(template)
        est = self.estimator.estimate(template, tier)
        res = RenderGate(ok=False, required_gib=round(est.peak_gib + self.cfg.memory_margin_gib, 1))
        start = time.monotonic()
        exclusive = model.exclusive_llm

        if exclusive and self.llm.loaded():
            if not self.llm.wait_idle(ANSWER_WAIT_S):  # 不打断正在写的回答
                res.actions.append("LLM still answering; gave up waiting")
                res.waited_s = round(time.monotonic() - start, 1)
                return res
            self.llm.unload()
            res.unloaded_llm = True
            res.actions.append(f"unloaded LLM {self.cfg.llm_model} (exclusive model)")
            self.log(res.actions[-1])
            self.sleep(2)

        switching = self.resident_family and self.resident_family != est.family
        if switching and not executor.busy() and executor.release():  # 换模型族：先释放上一族
            res.actions.append(f"released {self.resident_family} (switching to {est.family})")
            self.log(res.actions[-1])
            self.resident_family = None

        freed = False
        while True:
            busy = executor.busy()
            snap = self.snap()
            res.last = snap
            if not busy and snap["avail_gib"] >= res.required_gib and snap["pressure"] < 4:
                res.ok = True
                break
            if time.monotonic() - start > self.cfg.memory_wait_s:
                res.actions.append("timed out waiting for memory / an idle queue")
                break
            if busy:
                self.log(f"{executor.name} busy with other work; waiting")
            elif not freed:
                if executor.release():
                    res.actions.append(
                        f"released {executor.name} models "
                        f"(free {snap['avail_gib']} < {res.required_gib} GiB)"
                    )
                    self.log(res.actions[-1])
                freed = True
                self.sleep(3)
                continue
            elif not res.unloaded_llm and self.llm.loaded() and self.llm.wait_idle(60):
                self.llm.unload()
                res.unloaded_llm = True
                res.actions.append(
                    f"unloaded LLM (free {snap['avail_gib']} < {res.required_gib} GiB)"
                )
                self.log(res.actions[-1])
                self.sleep(2)
                continue
            self.sleep(10)
        res.waited_s = round(time.monotonic() - start, 1)
        if res.ok:
            with self._lock:
                self.render_active = {
                    "task_id": task_id,
                    "family": est.family,
                    "kind": template.kind,
                    "exclusive": exclusive,
                }
        return res

    def render_done(
        self,
        template: Template,
        released: bool,
        unloaded_llm: bool,
        on_restored: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            fam = self.render_active and self.render_active["family"]
            self.render_active = None
            self.resident_family = None if released else fam
        if unloaded_llm and self.cfg.llm_restore_after_video:
            try:
                self.llm.load(self.cfg.llm_idle_ttl_s)
                self.log(f"reloaded LLM (auto-unload after {self.cfg.llm_idle_ttl_s}s idle)")
                if on_restored:
                    on_restored()
            except Exception as e:  # noqa: BLE001 — 恢复失败不影响任务结果
                self.log(f"reloading LLM failed: {e}")
        for cb in list(self.listeners):
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                self.log(f"resource listener failed: {e}")

    # ---- 排队策略与 ETA ----
    def score(self, task: dict, now: float, family: str | None) -> float:
        t = self.registry.templates.get(task["template"])
        wait_min = max(0.0, (now - task["created"]) / 60)
        p = task.get("priority")
        s = (1 if p is None else p) * 1000 + min(wait_min * AGING_PER_MIN, AGING_CAP)
        if t and family and t.model == family:
            s += SAME_FAMILY_BONUS
        return s

    def choose_next(self, drafts: list[dict], family: str | None = None) -> dict | None:
        """优先级 > 等待时间老化 > 与常驻模型族相同；同分按创建时间与候选序号。"""
        if not drafts:
            return None
        now = time.time()
        fam = family if family is not None else self.resident_family
        return max(drafts, key=lambda t: (self.score(t, now, fam), -t["created"], -(t["idx"] or 0)))

    def plan(self, drafts: list[dict]) -> list[dict]:
        """按策略模拟出队顺序，给每个任务估计开始 / 结束时间（含切换代价）。不改共享状态。"""
        order, pending = [], list(drafts)
        with self._lock:
            fam, ra = self.resident_family, self.render_active
        clock = time.time() + (60 if ra else 0)  # 正在跑的任务：粗略按 1 分钟剩余计
        while pending:
            nxt = self.choose_next(pending, fam or "")
            pending.remove(nxt)
            t = self.registry.templates.get(nxt["template"])
            if t is None:
                continue
            est = self.estimator.estimate(t, nxt["tier"])
            switch = 15.0 if fam and fam != est.family else 0.0  # 释放 + 冷启动的粗略代价
            start = clock + switch
            end = start + est.exec_s
            order.append(
                {
                    "task_id": nxt["id"],
                    "template": t.name,
                    "tier": nxt["tier"],
                    "family": est.family,
                    "start": start,
                    "end": end,
                    "exec_s": round(est.exec_s, 1),
                    "source": est.source,
                }
            )
            clock, fam = end, est.family
        return order
