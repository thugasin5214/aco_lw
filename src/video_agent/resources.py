"""Dynamic resource scheduling (R1 + Q1, replacing memgate's fixed thresholds).

Principles (same as vhotseat):
- Running work is never interrupted: an answer the LLM is writing gets finished; renders are not preempted.
- Gate "before load / submit": ask `llm_admit` before LLM inference; go through `prepare_render` before submitting a render.
- Fewer hand-overs: jobs of the same model family run back to back; switching cost counts in the ordering.

Estimates come from historical metrics (per template + tier); without history, fall back to the model registry and tier cost ratios.
"""

from __future__ import annotations

import collections
import contextlib
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx

from . import docker, llm
from .memory import snapshot
from .registry import ModelInfo, Template

PRIORITY = {"interactive": 2, "agent": 1, "background": 0}
AGING_PER_MIN = 20  # points added per minute of waiting (prevents starvation)
# after 50 min background tasks can pass newer agent tasks, but never your interactive requests
AGING_CAP = 1500
# max wait for the LLM to finish its current answer (answers are not interrupted)
ANSWER_WAIT_S = 600
SAME_FAMILY_BONUS = 300  # same family as the resident model → saves a switch
LLM_LOAD_S = 15.4  # G0 measured
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
    """Learn from finished tasks' metrics (median, robust to outliers); extrapolate unmeasured tiers by tier cost ratios."""

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
        tier_peaks = [h["peak_gib"] for h in same if h["peak_gib"]]
        if model.adaptive or not tier_peaks:
            # adaptive engines use whatever is available, so the measured peak is not the minimum
            # need; without data for this tier use the registry value
            peak = model.peak_gib
        else:
            # per tier, so a big tier's peak does not raise a small tier's threshold
            peak = max(tier_peaks)
        if same:
            return Estimate(
                statistics.median(h["exec_s"] for h in same),
                peak,
                template.model,
                f"history n={len(same)}",
            )
        # extrapolate: other tiers of the same template scaled by cost ratio
        for h in hist:
            if h["tier"] in template.tiers:
                base = tier_cost(template.tiers[h["tier"]].values())
                return Estimate(
                    h["exec_s"] * cost / base, peak, template.model, f"scaled from {h['tier']}"
                )
        default = model.exec_s or DEFAULT_EXEC_S[template.kind]
        return Estimate(default, peak, template.model, "default")


class LLMWorkload:
    """The local LLM as a workload: whether it is loaded and how many inferences are in flight (never unloaded mid-inference)."""

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
        c = self.cfg
        llm.load(
            c.llm_model,
            c.llm_context,
            ttl,
            getattr(c, "llm_gpu", ""),
            getattr(c, "llm_parallel", 0),
        )


class ResourceBusy(RuntimeError):
    pass


@dataclass
class RenderGate:
    ok: bool
    required_gib: float
    actions: list[str] = field(default_factory=list)
    unloaded_llm: bool = False
    last: dict = field(default_factory=dict)
    docker: dict = field(default_factory=dict)  # Docker's share of unified memory at submit (read-only)
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
        self.listeners: list[Callable[[], None]] = []  # render finished → wake parked LLM jobs
        self.history: collections.deque = collections.deque(maxlen=720)  # about 1 hour (every 5 s)
        # read-only Docker attribution (visibility only; psutil already counts the VM in avail/used).
        # Injected like `executors` so tests can supply a fake probe.
        self.docker_probe: Callable[[], dict] = docker.docker_snapshot
        self.docker_aware: bool = getattr(cfg, "docker_aware", True)
        self.docker_poll_s: float = getattr(cfg, "docker_poll_s", 5.0)
        self._docker_cache: tuple[float, dict] | None = None
        # injected by the orchestrator: cross-process gating asks the executors directly
        self.executors: dict | None = None
        # FreeVideo films being shot (production film stage): they skip prepare_render but hold the
        # memory just the same
        self.films: set[str] = set()

        self._lock = threading.RLock()

    def record(self) -> None:
        """Called periodically by the heartbeat thread: records the resource curve (console Queue page)."""
        snap = self.snap()
        with self._lock:
            ra = self.render_active
        du = self.docker_usage()
        self.history.append(
            {
                "t": time.time(),
                **snap,
                "render": ra and ra["kind"],
                "llm": self.llm.in_flight > 0,
                "docker_gib": du.get("total_gib", 0.0) if du.get("available") else 0.0,
            }
        )

    @staticmethod
    def snap() -> dict:
        return snapshot()  # looked up at runtime so tests can replace the module-level snapshot

    def docker_usage(self) -> dict:
        """Read-only, cached: how much unified memory Docker holds, and per container.

        Attribution only — the admission math already sees Docker through psutil's
        avail/used, so this value is never subtracted again. Never raises; the probe runs
        at most once per `docker_poll_s`. Returns `docker.DockerSnapshot.as_dict()` shape.
        """
        if not self.docker_aware:
            return {"available": False, "total_gib": 0.0, "containers": [], "error": "disabled"}
        now = time.monotonic()
        cached = self._docker_cache
        if cached and now - cached[0] < self.docker_poll_s:
            return cached[1]
        try:
            usage = self.docker_probe()
        except Exception as e:  # noqa: BLE001 - a probe failure must never break scheduling
            usage = {"available": False, "total_gib": 0.0, "containers": [], "error": str(e)}
        self._docker_cache = (now, usage)
        return usage

    # ---- LLM gate ----
    def exclusive_classes(self) -> set[str]:
        """Node types used by memory-exclusive templates (e.g. FreeVideoGenerate), to recognise similar tasks submitted by others."""
        out: set[str] = set()
        for t in self.registry.templates.values():
            if self.registry.model_of(t).exclusive_llm:
                out |= {n["class_type"] for n in t.graph.values()}
        generic = {"LoadImage", "PreviewAny", "SaveImage", "CLIPTextEncode"}
        return out - generic

    def external_exclusive(self) -> str | None:
        """Cross-process gate (vhotseat's approach): ask the shared executor directly instead of trusting only this process's records.
        Exclusive tasks from any source count (this daemon, a dev / CLI process, your manual submissions in the ComfyUI page)."""
        now = time.monotonic()
        cached = getattr(self, "_ext_cache", None)
        if cached and now - cached[0] < 2:
            return cached[1]
        why = None
        classes = self.exclusive_classes()
        for ex in (self.executors or {}).values():
            probe = getattr(ex, "queued_payloads", None)
            if probe is None:
                continue
            try:
                payloads = probe()
            # executor unreachable: not treated as busy
            except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
                self.log(f"{ex.name} queue check failed: {e}")
                continue
            for graph in payloads:
                if any(n.get("class_type") in classes for n in graph.values()):
                    why = f"a video render in {ex.name} holds the memory"
                    break
        self._ext_cache = (now, why)
        return why

    def llm_admit(self) -> tuple[bool, str]:
        """Can the LLM run inference now? If not, the caller should park the job (non-blocking); it is woken when the render ends."""
        with self._lock:
            ra = self.render_active
            if ra and ra["exclusive"]:
                return False, f"{ra['kind']} render running ({ra['family']}) — needs the memory"
            if self.films:
                return False, "FreeVideo film shooting — needs the memory"
        ext = self.external_exclusive()
        if ext and not self.llm.loaded():
            # an already-loaded LLM is not unloaded here; this only blocks reloading during an
            # exclusive render
            return False, ext
        if self.snap()["pressure"] >= 4:
            return False, "memory pressure critical"
        return True, ""

    # ---- FreeVideo film gate (the production film stage calls the FreeVideo agent API directly,
    # not via prepare_render) ----
    def prepare_film(self) -> list[str]:
        """Before shooting / reshooting: wait for the LLM to finish its current answer, then unload it. Measured 2026-11-05: with the 27B LLM (25.5 GiB)
        still resident while FreeVideo (51 GiB) was shooting, Jetsam kicked in and the whole machine froze and rebooted."""
        actions: list[str] = []
        if self.llm.loaded():
            if not self.llm.wait_idle(ANSWER_WAIT_S):  # do not interrupt an answer being written
                raise ResourceBusy("LLM is still answering; start the film again in a moment")
            self.llm.unload()
            actions.append(f"unloaded LLM {self.cfg.llm_model} (FreeVideo film)")
            self.log(actions[-1])
        return actions

    def film_started(self, film_id: str) -> None:
        with self._lock:
            self.films.add(film_id)

    def films_shooting(self, film_ids: set[str]) -> None:
        """The film poller reports the films still shooting each round. While any is shooting: the LLM must not load, a resident one is unloaded once idle
        (covers continuing a shoot after a daemon restart, or the LLM being loaded elsewhere mid-shoot); when all are done: wake the waiters."""
        with self._lock:
            was, self.films = self.films, set(film_ids)
        if self.films and self.llm.loaded() and self.llm.wait_idle(0):
            self.llm.unload()
            self.log(f"unloaded LLM {self.cfg.llm_model} (FreeVideo film shooting)")
        if was and not self.films:
            for cb in list(self.listeners):
                try:
                    cb()
                except Exception as e:  # noqa: BLE001
                    self.log(f"resource listener failed: {e}")

    # ---- render gate (replaces memgate.ensure) ----
    def prepare_render(self, template: Template, tier: str, executor, task_id: str) -> RenderGate:
        model = self.registry.model_of(template)
        # does not use the bulk of unified memory: no waiting, no unloading, no render_active
        if model.resource == "cpu":
            return RenderGate(ok=True, required_gib=0, actions=["cpu job; memory gate skipped"])
        est = self.estimator.estimate(template, tier)
        res = RenderGate(ok=False, required_gib=round(est.peak_gib + self.cfg.memory_margin_gib, 1))
        start = time.monotonic()
        exclusive = model.exclusive_llm

        if exclusive and self.llm.loaded():
            if not self.llm.wait_idle(ANSWER_WAIT_S):  # do not interrupt an answer being written
                res.actions.append("LLM still answering; gave up waiting")
                res.waited_s = round(time.monotonic() - start, 1)
                return res
            self.llm.unload()
            res.unloaded_llm = True
            res.actions.append(f"unloaded LLM {self.cfg.llm_model} (exclusive model)")
            self.log(res.actions[-1])
            self.sleep(2)

        switching = self.resident_family and self.resident_family != est.family
        # switching model family: release the previous family first
        if switching and not executor.busy() and executor.release():
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
        res.docker = self.docker_usage()  # read-only attribution recorded at submit time
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
            # a failed restore does not affect the task result
            except Exception as e:  # noqa: BLE001
                self.log(f"reloading LLM failed: {e}")
        for cb in list(self.listeners):
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                self.log(f"resource listener failed: {e}")

    # ---- queue policy and ETA ----
    def score(self, task: dict, now: float, family: str | None) -> float:
        t = self.registry.templates.get(task["template"])
        wait_min = max(0.0, (now - task["created"]) / 60)
        p = task.get("priority")
        s = (1 if p is None else p) * 1000 + min(wait_min * AGING_PER_MIN, AGING_CAP)
        if t and family and t.model == family:
            s += SAME_FAMILY_BONUS
        return s

    def choose_next(self, drafts: list[dict], family: str | None = None) -> dict | None:
        """Priority > aging by waiting time > same family as the resident model; ties by creation time and candidate index."""
        if not drafts:
            return None
        now = time.time()
        fam = family if family is not None else self.resident_family
        return max(drafts, key=lambda t: (self.score(t, now, fam), -t["created"], -(t["idx"] or 0)))

    def plan(self, drafts: list[dict]) -> list[dict]:
        """Simulate the dequeue order by policy and estimate each task's start / end time (including switching cost). Does not change shared state."""
        order, pending = [], list(drafts)
        with self._lock:
            fam, ra = self.resident_family, self.render_active
        clock = time.time() + (60 if ra else 0)  # the running task: roughly 1 minute left
        while pending:
            nxt = self.choose_next(pending, fam or "")
            pending.remove(nxt)
            t = self.registry.templates.get(nxt["template"])
            if t is None:
                continue
            est = self.estimator.estimate(t, nxt["tier"])
            # rough cost of release + cold start
            switch = 15.0 if fam and fam != est.family else 0.0
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
