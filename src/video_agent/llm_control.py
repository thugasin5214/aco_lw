"""Switching / loading / unloading the local LLM on request (console, Telegram /llm, `va llm`).

One code path for every entry, so the resource state stays consistent:
- the switch waits for the current answer to finish and never runs while an exclusive render,
  a FreeVideo film or a memory-holding agent command is active;
- it checks memory first: available + what unloading the previous model frees ≥ new model + margin;
- the previous model (the one aco was using) is unloaded before the new one loads.
Loading runs in a background thread; `wait()` blocks until it is done (for the CLI).
"""

from __future__ import annotations

import threading
import time

from . import config, llm

# measured: a 16.2 GB 27B file used 19.3–19.9 GiB once loaded (G0); the ratio covers KV cache etc.
FOOTPRINT_RATIO = 1.25  # estimate


class LLMControlError(RuntimeError):
    pass


class LLMControl:
    def __init__(self, cfg, resources, planner):
        self.cfg, self.rm, self.planner = cfg, resources, planner
        self.job: dict = {"state": "idle", "error": None, "detail": None}
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()

    # ---- read ----
    def models(self) -> list[dict]:
        return llm.models()

    def status(self) -> dict:
        loaded = llm.loaded_all()
        return {
            "model": self.cfg.llm_model,
            "loaded": [
                {
                    "id": m.get("identifier"),
                    "context": m.get("contextLength"),
                    "ttl_ms": m.get("ttlMs"),
                }
                for m in loaded
            ],
            "models": self.models(),
            "job": dict(self.job),
        }

    def resolve(self, name: str) -> str:
        """Model key by exact key, list index (1-based) or a unique case-insensitive substring."""
        models = [m["key"] for m in self.models()]
        if name.isdigit() and 0 < int(name) <= len(models):
            return models[int(name) - 1]
        exact = [k for k in models if k.lower() == name.lower()]
        if exact:
            return exact[0]
        hits = [k for k in models if name.lower() in k.lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LLMControlError(
                f"no downloaded model matches {name!r}; have: {', '.join(models)}"
            )
        raise LLMControlError(f"{name!r} is ambiguous: {', '.join(hits)}")

    def need_gib(self, key: str) -> float:
        m = next((m for m in self.models() if m["key"] == key), None)
        size = (m or {}).get("size") or 0
        return round(size / 2**30 * FOOTPRINT_RATIO, 1)

    def _loaded_gib(self, key: str) -> float:
        return self.need_gib(key) if llm.loaded(key) else 0.0

    # ---- act ----
    def _check(self, new: str, prev: str | None) -> None:
        if self.job["state"] == "working":
            raise LLMControlError("a load / unload is still in progress")
        ok, why = self.rm.llm_admit()
        if not ok:
            raise LLMControlError(f"not now: {why}")
        need = self.need_gib(new) + self.cfg.memory_margin_gib
        frees = self._loaded_gib(prev) if prev and prev != new else 0.0
        avail = self.rm.snap()["avail_gib"]
        if need and avail + frees < need:
            raise LLMControlError(
                f"not enough memory for {new}: needs ~{need:.0f} GiB (estimate), "
                f"{avail:.0f} GiB available + {frees:.0f} GiB from unloading {prev}"
            )
        if not self.rm.llm.wait_idle(0):
            raise LLMControlError("the LLM is answering; try again when it is done")

    def _ttl(self) -> int | None:
        return self.cfg.llm_session_ttl_s if self.planner.mode == "on_demand" else None

    def _run(self, fn, detail: str) -> None:
        self.job.update(state="working", error=None, detail=detail)
        self._done.clear()

        def go() -> None:
            t = time.monotonic()
            try:
                fn()
                self.job.update(state="idle", seconds=round(time.monotonic() - t, 1))
            except Exception as e:  # noqa: BLE001 — reported through job / the caller
                self.job.update(state="error", error=str(e))
            finally:
                self._done.set()

        threading.Thread(target=go, name="llm-control", daemon=True).start()

    def use(self, name: str) -> dict:
        """Switch aco's local model: unload the previous one, load the new one, save to config."""
        with self._lock:
            new = self.resolve(name)
            prev = self.cfg.llm_model
            if new == prev and llm.loaded(new):
                return {"state": "loaded", "model": new}
            self._check(new, prev)
            self.cfg.llm_model = new
            if self.cfg.config_path:
                config.save({"llm_model": new}, self.cfg.config_path)
            ttl = self._ttl()

            def job() -> None:
                if prev and prev != new and llm.loaded(prev):
                    llm.unload(prev)
                if not llm.loaded(new):
                    self.rm.llm.load(ttl)

            self._run(job, f"{prev} → {new}")
            return {"state": "working", "from": prev, "model": new, "need_gib": self.need_gib(new)}

    def load(self, reload: bool = False) -> dict:
        with self._lock:
            model = self.cfg.llm_model
            if llm.loaded(model) and not reload:
                return {"state": "loaded", "model": model}
            self._check(model, model if reload else None)
            ttl = self._ttl()

            def job() -> None:
                if llm.loaded(model):
                    llm.unload(model)
                self.rm.llm.load(ttl)

            self._run(job, f"load {model}")
            return {"state": "working", "model": model, "need_gib": self.need_gib(model)}

    def unload(self) -> dict:
        model = self.cfg.llm_model
        if not llm.loaded(model):
            return {"state": "unloaded", "model": model}
        if not self.rm.llm.wait_idle(0):
            raise LLMControlError("the LLM is answering; try again when it is done")
        llm.unload(model)
        return {"state": "unloaded", "model": model}

    def wait(self, timeout: float = 600) -> dict:
        self._done.wait(timeout)
        return dict(self.job)
