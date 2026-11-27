"""Production pipeline (stage 3): declarative workflows + an artifact store with versions and staleness propagation + an authoritative plan + stage execution.

Novel → finished film is just one YAML set in `workflows/`; another set (ads, music videos, ...) needs no code change.

- Workflow spec: a DAG of stages. Stage kinds: input (text the user gives) / skill (the LLM produces JSON per SKILL.md,
  schema-validated) / render (images / videos from templates, can fan out over an upstream list) / assemble (hands the edit timeline to FFmpeg).
- Artifacts: each (stage, key) has several versions; each records the upstream artifacts it was made from (version or item fingerprint).
  When an upstream gets a new version, dependants are **computed** as stale, no cascading writes; changing one character only affects the shots that use it.
- Plan: each stage's state and next_action computed deterministically from spec + artifacts. The console, Telegram, the agent and
  Claude Code all see the same plan; the agent keeps no state machine of its own.
- Render stages reuse the existing "round / candidates / review": picking one approves it; new rounds from redo / refine automatically follow the same artifact.
- The LLM call of skill stages is injected by the caller (the daemon picks a backend by resource gate and fallback chain); short of resources they queue
  and run after the render ends.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .executors.ffmpeg import probe_seconds

RUNNING = ("queued", "running")
ACTIVE_TASK = ("submitted", "running")  # renders ComfyUI / FreeVideo is working on


# ---------------------------------------------------------------- spec
class FanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: str  # list path in upstream data, e.g. "bible.characters"
    key: str = "id"


class ChunkBy(BaseModel):
    """A skill stage whose output is too long for one LLM reply (e.g. a 5-minute storyboard): call
    the skill once per upstream item and concatenate one list field of the replies."""

    model_config = ConfigDict(extra="forbid")

    items: str  # list path, e.g. "adaptation.scenes"
    merge: str  # the list field joined across replies, e.g. "shots"
    # merged elements are renumbered with this format (1-based), e.g. "sh{n:02d}"
    id_format: str | None = None
    # a setting in minutes (e.g. "episode_minutes"): each item gets a share of it as target_seconds,
    # weighted by the length of `weight` in the item (e.g. its beats)
    budget_minutes: str | None = None
    weight: str | None = None
    # upstream stages left out of each call's inputs (e.g. the whole novel when the item is a
    # chapter of it)
    omit_inputs: list[str] = []


class Cap(BaseModel):
    """A number in every element of a list in the output must not exceed a setting, e.g. shot
    seconds ≤ settings.max_clip_seconds (one generation renders only so many seconds)."""

    model_config = ConfigDict(extra="forbid")

    list: str
    field: str
    setting: str


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str
    params: dict[str, Any] = {}
    tier: str | None = None  # may be "{settings.video_tier}"
    n: int | str | None = None


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # path in the context, e.g. ref.use
    equals: Any = None  # omitted: the path's value just has to be truthy


class Alternative(BaseModel):
    """Conditional branches of a render item (the first match wins): take an image as-is (provide), or render differently."""

    model_config = ConfigDict(extra="forbid")

    when: When
    # image path expression, e.g. "{ref.path}": used as-is as an approved artifact
    provide: str | None = None
    render: RenderSpec | None = None


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    kind: Literal["input", "tool", "skill", "render", "assemble", "film"]
    # film: which stage the character cards come from ({"characters": {key: card id}})
    cast: str | None = None
    # tool stage: a deterministic tool registered in the orchestrator (e.g. ingest)
    tool: str | None = None
    needs: list[str] = []
    skill: str | None = None
    # reference skills read alongside (e.g. MiniMax's official h3-prompt-writing); skipped if not
    # installed
    extra_skills: list[str] = []
    # a specific backend (e.g. claude_code); empty uses the production setting / primary backend
    llm: str | None = None
    instructions: str = ""
    output_schema: dict | str | None = Field(None, alias="schema")
    fan_out: FanOut | None = None
    chunk_by: ChunkBy | None = None  # skill: one LLM call per item, replies merged
    cap: Cap | None = None  # skill: a per-element limit checked like the schema (one repair)
    # skill: pass only these fields of an input (e.g. {brief: [references]} — the photos, not the
    # whole novel)
    pick: dict[str, list[str]] = {}
    render: RenderSpec | None = None
    alternatives: list[Alternative] = []
    timeline: str | None = None  # assemble: which stage the timeline comes from
    clips: str | None = None  # assemble: which stage the clips come from
    gate: Literal["review", "auto"] = "review"
    # True: do not start automatically after the previous stage is approved (e.g. video, hours
    # long); wait for you to press start
    confirm: bool = False


RULE_KEYS = ("cascade", "on_reject", "guidance")


class Rules(BaseModel):
    """How a workflow is run (workflow mode). Defaults here, then the workflow's YAML, then
    data/workflow_rules.yaml (per workflow name), then the production's own settings["rules"];
    each may also override single stages under `stages: {stage_id: {...}}`."""

    model_config = ConfigDict(extra="forbid")

    # stopping or rejecting an item also stops everything after it (downstream stages: queued
    # work dropped, running renders interrupted, skill jobs dropped); they wait until it is
    # approved again
    cascade: bool = True
    # a rejected render item without a note: re-render it with new seeds, or wait for you
    on_reject: Literal["rerender", "wait"] = "rerender"
    # how this workflow wants to be worked on: shown to the agent with the plan
    guidance: str = ""
    stages: dict[str, dict[str, Any]] = {}


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    description: str = ""
    settings: dict[str, Any] = {}
    rules: Rules = Rules()
    stages: list[Stage]

    @model_validator(mode="after")
    def _check(self):
        seen: set[str] = set()
        for st in self.stages:
            if st.id in seen:
                raise ValueError(f"阶段 {st.id} 重复")
            missing = [n for n in st.needs if n not in seen]
            if missing:
                raise ValueError(f"阶段 {st.id} 依赖未定义或排在后面的阶段：{missing}")
            if st.kind == "skill" and not (st.skill and st.output_schema):
                raise ValueError(f"skill 阶段 {st.id} 需要 skill 与 schema")
            if st.kind == "film" and not st.fan_out:
                raise ValueError(f"film 阶段 {st.id} 需要 fan_out（按镜头展开）")
            if st.kind == "tool" and not st.tool:
                raise ValueError(f"tool 阶段 {st.id} 需要 tool")
            if st.kind == "render" and not st.render:
                raise ValueError(f"render 阶段 {st.id} 需要 render")
            if st.kind == "assemble" and not (st.timeline and st.clips):
                raise ValueError(f"assemble 阶段 {st.id} 需要 timeline 与 clips")
            seen.add(st.id)
        return self

    def stage(self, sid: str) -> Stage:
        for st in self.stages:
            if st.id == sid:
                return st
        raise KeyError(f"工作流 {self.name} 没有阶段 {sid}")


def load_workflows(*roots: Path | None) -> dict[str, Workflow]:
    out: dict[str, Workflow] = {}
    for root in roots:
        if not root or not root.is_dir():
            continue
        for p in sorted(root.glob("*.yaml")):
            raw = yaml.safe_load(p.read_text())
            # schema may be a file name: workflows/schemas/<name>.json
            for st in raw.get("stages", []):
                sch = st.get("schema")
                if isinstance(sch, str):
                    st["schema"] = json.loads((root / "schemas" / f"{sch}.json").read_text())
            wf = Workflow(**raw)
            out[wf.name] = wf
    return out


# ---------------------------------------------------------------- expressions
_REF = re.compile(r"\{([A-Za-z_][\w.\-]*)\}")


def lookup(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def interp(value: Any, ctx: dict) -> Any:
    """Replace {a.b.c} in a string from the context; when the whole string is one reference, keep the original type."""
    if isinstance(value, dict):
        return {k: interp(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [interp(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    m = _REF.fullmatch(value)
    if m:
        return lookup(ctx, m.group(1))

    def sub(m: re.Match) -> str:
        v = lookup(ctx, m.group(1))
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    return _REF.sub(sub, value)


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[
        :12
    ]


def extract_json(text: str) -> Any:
    """Extract JSON from a model reply (tolerates ```json fences and surrounding text)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start : end + 1])
        raise


class Park(Exception):
    """The LLM cannot run now (short of resources and no usable fallback): the skill job stays queued and retries once resources free up."""


class ProductionError(RuntimeError):
    pass


# ---------------------------------------------------------------- engine
class Production:
    """Production engine. `llm(stage, messages, schema) -> (text, stats)`; raises Park when it cannot run."""

    def __init__(
        self,
        studio,
        skills,
        workflows: dict[str, Workflow],
        llm: Callable[[Stage, list[dict], dict], tuple[str, dict]] | None = None,
        log: Callable[[str], None] = print,
        work_dir: Path | None = None,
        tools: dict[str, Callable] | None = None,
        freevideo=None,
    ):
        self.studio, self.store, self.skills = studio, studio.store, skills
        # deterministic tools of tool stages: fn(production, stage, inputs) -> data; more can be
        # registered
        self.tools: dict[str, Callable] = {"ingest": self._tool_ingest, **(tools or {})}
        from .series import SeriesTools

        self.series = SeriesTools(self)
        self.tools.setdefault("chapters", self.series.chapters)
        self.tools.setdefault("series_spawn", self.series.spawn)
        # film stage (FreeVideo multi-shot film); without FreeVideo these stages cannot start
        self.film = None
        if freevideo is not None:
            from .film import FilmDriver

            self.film = FilmDriver(self, freevideo)
            self.tools.setdefault("freevideo_cards", self.film.cards_tool)
        self.workflows, self.llm, self.log = workflows, llm, log
        self.work_dir = work_dir or (self.store.jobs_dir.parent / "productions")
        # a stage enters review / fails / completes → notify
        self.listeners: list[Callable[[dict], None]] = []
        # a production was deleted → its id (agent runs waiting on it are woken)
        self.deleted_listeners: list[Callable[[str], None]] = []
        self.stop_running: Callable[[str], dict | None] = lambda pid: (
            None
        )  # injected by the daemon: interrupt this production's running task
        self._lock = threading.RLock()
        self._wake = threading.Event()
        studio.round_listeners.append(self._on_round_created)
        studio.decision_listeners.append(self._on_round_decided)
        studio.stop_listeners.append(self._on_round_stopped)
        self.rules_path: Path | None = None  # data/workflow_rules.yaml (set by the daemon)

    # ---- create ----
    def create(
        self,
        workflow: str,
        title: str,
        inputs: dict[str, Any],
        settings: dict | None = None,
        project_id: str | None = None,
    ) -> dict:
        if workflow not in self.workflows:
            raise ProductionError(f"未知工作流 {workflow}；可选：{', '.join(self.workflows)}")
        wf = self.workflows[workflow]
        pid = project_id or self.studio.current_project()["id"]
        merged = {**wf.settings, **(settings or {})}
        prod = self.store.create_production(pid, workflow, title, merged)
        for st in wf.stages:
            if st.kind == "input":
                if st.id not in inputs:
                    raise ProductionError(f"缺少输入 {st.id}")
                self.store.add_artifact(prod["id"], st.id, "main", "current", data=inputs[st.id])
        self.advance(prod["id"])
        return prod

    def update_settings(self, production_id: str, changes: dict) -> dict:
        """Change production settings (e.g. dialogue language, video tier). Approved artifacts are not rerun automatically — items started next use the new settings."""
        prod = self._prod(production_id)
        return self.store.update_production(production_id, settings={**prod["settings"], **changes})

    # ---- pause / resume ----
    def task_production(self, task: dict) -> str | None:
        art = self.store.artifact_by_round(task["round_id"]) if task.get("round_id") else None
        return art and art["production_id"]

    def holds(self, task: dict) -> bool:
        """The Worker asks before taking a task: if its production is paused, do not run it yet (the queue stays and runs after resume)."""
        if task.get("template") == "freevideo-take":
            return True  # FreeVideo takes are shot by FreeVideo itself, not the Worker
        pid = self.task_production(task)
        if pid is None:
            return False
        prod = self.store.get_production(pid)
        return bool(prod and prod["state"] == "paused")

    def pause(self, production_id: str, stop_running: bool = False) -> dict:
        """Pause: no new items start, queued renders and LLM jobs wait; optionally interrupt the running one right away."""
        prod = self._prod(production_id)
        if prod["state"] not in ("active", "done", "paused"):
            raise ProductionError(f"制片状态 {prod['state']}，不能暂停")
        prod = self.store.update_production(production_id, state="paused")
        stopped = self.stop_running(production_id) if stop_running else None
        if stop_running and self.film is not None:
            self.film.cancel(production_id)  # cancel takes not yet shot (finished ones are kept)
        self.log(
            f"production {production_id}: paused"
            + (f", interrupted {stopped['id']}" if stopped else "")
        )
        return {**prod, "interrupted": stopped and stopped["id"]}

    def resume(self, production_id: str) -> dict:
        prod = self._prod(production_id)
        if prod["state"] != "paused":
            raise ProductionError("这个制片没有暂停")
        prod = self.store.update_production(production_id, state="active")
        plan = self.plan(production_id)  # may already be complete
        # render items that failed because pausing interrupted them: rerun once on resume
        for s in plan["stages"]:
            failed = [i["key"] for i in s["items"] if i["state"] == "failed"]
            if s["kind"] == "render" and failed and s["state"] != "blocked":
                self.run_stage(production_id, s["id"], failed, "resumed after pause", "production")
        self.advance(production_id)
        self._wake.set()
        self.log(f"production {production_id}: resumed")
        return self.store.get_production(production_id)

    # ---- delete ----
    def delete(self, production_id: str) -> dict:
        """Delete a production for real: its series episodes, the render rounds it created (with
        their files), its artifacts and its work directory. Unsubmitted drafts are dropped; refused
        while ComfyUI / FreeVideo runs one of its renders (pause with "stop now" first). An LLM call in flight is dropped when it
        returns. Agents waiting on it are woken with `deleted`."""
        from .i18n import tr

        prod = self._prod(production_id)
        family = [*self.series.episodes(prod), prod]
        with self._lock:
            rounds = {p["id"]: self.store.production_rounds(p["id"]) for p in family}
            tasks = {
                p["id"]: [t for r in rounds[p["id"]] for t in self.store.round_tasks(r["id"])]
                for p in family
            }
            # drafts not yet submitted are simply dropped (paused → the Worker no longer takes
            # them); only what ComfyUI / FreeVideo is already working on blocks
            busy = [t for ts in tasks.values() for t in ts if t["state"] in ACTIVE_TASK]
            if busy:
                raise ProductionError(tr("prod_delete_busy", n=len(busy)))
            for p in family:
                self.store.update_production(p["id"], state="paused")
            if self.film is not None:
                for p in family:
                    self.film.cancel(p["id"])  # FreeVideo takes not yet shot
            out = {"productions": 0, "artifacts": 0, "rounds": 0, "tasks": 0, "freed_mb": 0.0}
            for p in family:
                freed = sum(self.studio._remove_files(t) for t in tasks[p["id"]])
                self.store.delete_tasks([t["id"] for t in tasks[p["id"]]])
                for r in rounds[p["id"]]:
                    self.store.delete_round_row(r["id"])
                out["artifacts"] += self.store.delete_production_rows(p["id"])
                shutil.rmtree(self.work_dir / p["id"], ignore_errors=True)
                out["productions"] += 1
                out["rounds"] += len(rounds[p["id"]])
                out["tasks"] += len(tasks[p["id"]])
                out["freed_mb"] += round(freed / 2**20, 1)
                for cb in list(self.deleted_listeners):
                    try:
                        cb(p["id"])
                    except Exception as e:  # noqa: BLE001
                        self.log(f"production delete listener failed: {e}")
        self.log(f"production {production_id}: deleted ({out})")
        return {**out, "title": prod["title"]}

    def _gone(self, production_id: str) -> bool:
        return self.store.get_production(production_id) is None

    # ---- rules (workflow mode) ----
    def user_rules(self) -> dict:
        path = getattr(self, "rules_path", None)
        if path is None or not Path(path).exists():
            return {}
        data = yaml.safe_load(Path(path).read_text()) or {}
        return data if isinstance(data, dict) else {}

    def rules(self, prod: dict, stage_id: str | None = None) -> dict:
        wf = self._wf(prod)
        layers = [
            wf.rules.model_dump(),
            self.user_rules().get(wf.name) or {},
            prod["settings"].get("rules") or {},
        ]
        out: dict[str, Any] = {k: v for k, v in Rules().model_dump().items() if k in RULE_KEYS}
        for layer in layers:
            out.update({k: v for k, v in layer.items() if k in RULE_KEYS})
        if stage_id:
            for layer in layers:
                out.update(
                    {
                        k: v
                        for k, v in ((layer.get("stages") or {}).get(stage_id) or {}).items()
                        if k in RULE_KEYS
                    }
                )
        return Rules(**out).model_dump(exclude={"stages"})

    @staticmethod
    def downstream(wf: Workflow, stage_id: str) -> list[str]:
        """Stages that (directly or through others) need this one, in workflow order."""
        after, out = {stage_id}, []
        for st in wf.stages:
            if st.id != stage_id and set(st.needs) & after:
                after.add(st.id)
                out.append(st.id)
        return out

    def stop_downstream(self, production_id: str, stage_id: str, reason: str) -> dict:
        """Stop everything after this stage: queued and running renders of downstream stages are
        stopped, their queued / running skill jobs dropped, a downstream FreeVideo film cancelled.
        They stay stopped until the upstream item is approved again (then they are stale and
        start as usual)."""
        prod = self._prod(production_id)
        wf = self._wf(prod)
        stages = self.downstream(wf, stage_id)
        out = {"stages": stages, "rounds": 0, "jobs": 0}
        if not stages:
            return out
        note = f"stopped: {reason}"
        for r in self.store.production_rounds(production_id):
            sid = r["request"].split(":")[2] if r["request"].count(":") >= 3 else None
            if sid in stages and r["state"] == "generating":
                try:
                    self.studio.stop_round(r["id"], cascading=True)
                    a = self.store.artifact_by_round(r["id"])
                    if a is not None:  # waits for you; no auto-pick of what had finished
                        self.store.update_artifact(
                            a["id"], meta={**(a["meta"] or {}), "stopped": True}, note=note
                        )
                    out["rounds"] += 1
                except Exception as e:  # noqa: BLE001 — keep stopping the rest
                    self.log(f"production {production_id}: stopping round {r['id']} failed: {e}")
        for sid in stages:
            st = wf.stage(sid)
            for a in self.store.artifacts(production_id, sid):
                if a["round_id"] or a["status"] not in ("queued", "running"):
                    continue
                if st.kind == "film":
                    continue  # handled by the film below
                self.store.update_artifact(a["id"], status="rejected", note=note)
                out["jobs"] += 1
            if st.kind == "film" and self.film is not None:
                self.film.cancel(production_id)
        if out["rounds"] or out["jobs"]:
            self.log(f"production {production_id}: {note} → {out}")
        return out

    def _cascade(self, art: dict, reason: str) -> None:
        prod = self._prod(art["production_id"])
        if self.rules(prod, art["stage"])["cascade"]:
            res = self.stop_downstream(prod["id"], art["stage"], reason)
            if res["rounds"] or res["jobs"]:
                from .i18n import tr

                self.studio.log(
                    "production",
                    "out",
                    tr("prod_cascade", s=art["stage"], n=res["rounds"] + res["jobs"]),
                    prod["project_id"],
                )

    def _on_round_stopped(self, rnd: dict) -> None:
        """You stopped a production round (⏹): this item waits for you (no auto-pick of what
        finished), and by the rules everything after it stops too."""
        art = self.store.artifact_by_round(rnd["id"])
        if art is None:
            return
        self.store.update_artifact(art["id"], meta={**(art["meta"] or {}), "stopped": True})
        self._cascade(art, f"{art['stage']}/{art['key']} was stopped")

    @staticmethod
    def _gate(prod: dict, st: Stage) -> str:
        """auto when the workflow says so or the production lists the stage in auto_approve."""
        auto = st.id in (prod["settings"].get("auto_approve") or [])
        return "auto" if st.gate == "auto" or auto else "review"

    @staticmethod
    def _confirm(prod: dict, st: Stage) -> bool:
        """confirm stages wait for you to press start unless the production lists them in auto_start."""
        return st.confirm and st.id not in (prod["settings"].get("auto_start") or [])

    def _wf(self, prod: dict) -> Workflow:
        return self.workflows[prod["workflow"]]

    def _prod(self, production_id: str) -> dict:
        prod = self.store.get_production(production_id)
        if prod is None:
            raise ProductionError(f"找不到制片 {production_id}")
        return prod

    # ---- artifact index ----
    def _sync(self, art: dict) -> dict:
        """A render artifact's status is derived from its round (review may happen in the console, Telegram or the agent, and may be reopened)."""
        if not art["round_id"] or art["status"] == "superseded":
            return art
        rnd = self.store.get_round(art["round_id"])
        if rnd is None:
            return art
        d = rnd.get("decision") or {}
        if rnd["state"] == "generating":
            status, task_id = "running", None
        elif rnd["state"] == "review":
            tasks = self.store.round_tasks(rnd["id"])
            ok = [t for t in tasks if t["outputs"]]
            status, task_id = ("review" if ok else "failed"), None
        elif d.get("action") == "select":
            status, task_id = "current", d.get("task_id")
        elif d.get("action") in ("redo", "refine"):
            status, task_id = "superseded", None
        else:
            status, task_id = "rejected", None
        if status != art["status"] or task_id != art["task_id"]:
            data = art["data"]
            if task_id:
                task = self.store.get(task_id)
                data = {
                    "task_id": task_id,
                    "outputs": [
                        {**o, "path": str(self.store.task_dir(task_id) / o["file"])}
                        for o in task["outputs"]
                    ],
                    "params": task["params"],
                }
            art = self.store.update_artifact(art["id"], status=status, task_id=task_id, data=data)
        return art

    def _index(self, production_id: str) -> dict[tuple[str, str], list[dict]]:
        idx: dict[tuple[str, str], list[dict]] = {}
        for a in self.store.artifacts(production_id):
            idx.setdefault((a["stage"], a["key"]), []).append(self._sync(a))
        return idx

    @staticmethod
    def _current(idx, stage: str, key: str = "main") -> dict | None:
        cur = [a for a in idx.get((stage, key), []) if a["status"] == "current"]
        return cur[-1] if cur else None

    @staticmethod
    def _latest(idx, stage: str, key: str) -> dict | None:
        live = [a for a in idx.get((stage, key), []) if a["status"] != "superseded"]
        return live[-1] if live else None

    def _stage_data(self, wf: Workflow, idx, sid: str) -> Any:
        """A stage's data: single-artifact stages give data; fanned-out stages give {key: data}."""
        st = wf.stage(sid)
        if st.fan_out is None:
            cur = self._current(idx, sid)
            return cur["data"] if cur else None
        return {
            k: self._current(idx, s, k)["data"]
            for (s, k) in idx
            if s == sid and self._current(idx, s, k)
        }

    def _ctx(self, prod: dict, wf: Workflow, idx) -> dict:
        lang = (prod["settings"].get("language") or "").strip()
        ctx = {
            # speech_hint: derived from language, used in video prompts (H3 generates voices)
            "settings": {
                **prod["settings"],
                "speech_hint": f" Dialogue and speech in {lang}." if lang else "",
            },
            "production": {"id": prod["id"], "title": prod["title"]},
        }
        for st in wf.stages:
            ctx[st.id] = self._stage_data(wf, idx, st.id)
        return ctx

    def _items(self, st: Stage, ctx: dict) -> list[tuple[str, dict]]:
        items = lookup(ctx, st.fan_out.items) or []
        return [(str(it[st.fan_out.key]), it) for it in items if isinstance(it, dict)]

    def _stale(self, idx, art: dict) -> bool:
        for d in art["deps"]:
            cur = self._current(idx, d["stage"], d["key"])
            if cur is None:
                return True
            if d.get("any"):
                continue
            if "item" in d:
                lst = lookup({"x": cur["data"]}, "x." + d["path"]) or []
                item = next((i for i in lst if str(i.get(d["field"])) == d["item"]), None)
                if item is None or fingerprint(item) != d["hash"]:
                    return True
            elif cur["version"] != d["version"]:
                return True
        return False

    # ---- plan (authoritative, deterministic) ----
    def plan(self, production_id: str) -> dict:
        with self._lock:
            prod = self._prod(production_id)
            wf = self._wf(prod)
            idx = self._index(production_id)
            ctx = self._ctx(prod, wf, idx)
            stages, completed = [], set()
            for st in wf.stages:
                blocked_by = [n for n in st.needs if n not in completed]
                keys: list[str] = []
                if not blocked_by:
                    keys = [k for k, _ in self._items(st, ctx)] if st.fan_out else ["main"]
                items = []
                for k in keys:
                    latest = self._latest(idx, st.id, k)
                    cur = self._current(idx, st.id, k)
                    if latest is None:
                        state = "missing"
                    elif latest["status"] in RUNNING:
                        state = "running"
                    elif latest["status"] == "review":
                        state = "review"
                    elif latest["status"] == "current":
                        state = "stale" if self._stale(idx, latest) else "current"
                    else:
                        state = latest["status"]  # failed / invalid / rejected
                    items.append(
                        {
                            "key": k,
                            "state": state,
                            "version": latest and latest["version"],
                            "current_version": cur and cur["version"],
                            "artifact_id": latest and latest["id"],
                            "round_id": latest and latest["round_id"],
                            "note": latest and latest["note"],
                        }
                    )
                states = {i["state"] for i in items}
                if blocked_by:
                    sstate = "blocked"
                # upstream has no items (e.g. no characters): counts as complete
                elif st.fan_out and not items:
                    sstate = "completed"
                    completed.add(st.id)
                elif states & {"running"}:
                    sstate = "active"
                elif states & {"review"}:
                    sstate = "review"
                elif states <= {"current"}:
                    sstate = "completed"
                    completed.add(st.id)
                else:
                    sstate = "ready"
                stages.append(
                    {
                        "id": st.id,
                        "title": st.title or st.id,
                        "kind": st.kind,
                        "needs": st.needs,
                        "state": sstate,
                        "blocked_by": blocked_by,
                        "gate": self._gate(prod, st),
                        "confirm": self._confirm(prod, st),
                        "items": items,
                        "counts": {s: sum(1 for i in items if i["state"] == s) for s in states},
                    }
                )
            nxt = {"type": "none"}
            for s in stages:
                if s["state"] == "completed":
                    continue
                todo = [
                    i["key"]
                    for i in s["items"]
                    if i["state"] not in ("current", "running", "review")
                ]
                stale = [i["key"] for i in s["items"] if i["state"] == "stale"]
                if s["kind"] == "skill" and stale and s["state"] == "ready":
                    # upstream changed: keep it (confirm it still applies) or rerun — your call,
                    # since rerunning changes downstream
                    nxt = {"type": "stale", "stage": s["id"], "options": ["keep", "redo"]}
                elif s["state"] == "review" or (s["state"] == "active" and not todo):
                    kind = "review" if s["state"] == "review" else "wait"
                    nxt = {"type": kind, "stage": s["id"]}
                elif s["state"] in ("ready", "active"):
                    nxt = {
                        "type": "run_stage",
                        "stage": s["id"],
                        "keys": todo,
                        "confirm": s["confirm"],
                    }
                else:
                    nxt = {"type": "wait", "stage": s["id"]}
                break
            done = all(s["state"] == "completed" for s in stages)
            if done and prod["state"] == "active":
                prod = self.store.update_production(production_id, state="done")
            # rerun / replaced image / added stage after completion: back to active
            elif not done and prod["state"] == "done":
                prod = self.store.update_production(production_id, state="active")
                self._wake.set()  # its queued jobs can run now
            result = {
                "production": prod,
                "workflow": {"name": wf.name, "title": wf.title},
                "rules": self.rules(prod),
                "stages": stages,
                "next_action": nxt,
                "done": done,
            }
            if prod["settings"].get("series"):
                self.series.handoff(prod, result)
            return result

    # ---- execute ----
    def run_stage(
        self,
        production_id: str,
        stage_id: str,
        keys: list[str] | None = None,
        note: str | None = None,
        origin: str = "web",
        overrides: dict | None = None,
    ) -> list[dict]:
        """Start a stage (by default only missing / stale / failed items). Returns the new artifacts.
        overrides (film stage): {quality, takes} for this run only; then items in review can also be shot again for comparison."""
        with self._lock:
            prod = self._prod(production_id)
            wf = self._wf(prod)
            st = wf.stage(stage_id)
            plan = self.plan(production_id)
            ps = next(s for s in plan["stages"] if s["id"] == stage_id)
            if ps["state"] == "blocked":
                raise ProductionError(f"{stage_id} 还在等上游：{', '.join(ps['blocked_by'])}")
            if st.kind == "input":
                raise ProductionError("输入阶段不需要运行；要改原文请新建一个版本")
            if keys is not None and st.kind in ("skill", "tool"):
                keys = ["main"]  # one artifact per stage, whatever key was asked for
            busy_states = ("running",) if overrides else ("running", "review")
            busy = {i["key"] for i in ps["items"] if i["state"] in busy_states}
            if keys is None:
                keys = [
                    i["key"]
                    for i in ps["items"]
                    if i["state"] not in ("current", "running", "review")
                ]
            elif busy & set(keys):
                running = [
                    a
                    for k in busy & set(keys)
                    if (a := self._latest(self._index(production_id), stage_id, k))
                    and a["status"] in RUNNING
                ]
                since = (
                    f"；已运行 {int((time.time() - min(a['created'] for a in running)) / 60)} 分钟"
                    "（LLM 写长内容常要 10–20 分钟，不是卡死，等它完成）"
                    if running
                    else ""
                )
                raise ProductionError(
                    f"这些条目正在生成或待审阅：{', '.join(sorted(busy & set(keys)))}{since}"
                )
            idx = self._index(production_id)
            ctx = self._ctx(prod, wf, idx)
            made = []
            if st.kind == "film":
                if self.film is None:
                    raise ProductionError("film 阶段需要 FreeVideo（freevideo_agent 未启用）")
                if keys:
                    made = self.film.start(
                        prod,
                        st,
                        idx,
                        ctx,
                        keys,
                        note,
                        lambda k: self._item_deps(prod, st, idx, ctx, k),
                        overrides,
                    )
                keys = []
            for k in keys:
                if st.kind in ("skill", "tool"):
                    made.append(self._queue_skill(prod, wf, st, idx, note))
                elif st.kind == "render":
                    made.append(self._start_render(prod, st, idx, ctx, k, origin))
                else:
                    made.append(self._start_assemble(prod, wf, st, idx, ctx, origin))
            self.log(f"production {production_id}: {stage_id} started ({len(made)} item(s))")
            self._wake.set()
            # some item took your image directly: may already be able to advance
            if any(m["status"] == "current" for m in made):
                self.advance(production_id)
            return made

    def _whole_deps(self, wf: Workflow, st: Stage, idx) -> list[dict]:
        deps = []
        for n in st.needs:
            # planning stages read "which items are approved" from fanned-out render stages, not the
            # pixels: replacing a casting image does not make the storyboard stale
            loose = wf.stage(n).kind in ("render", "assemble") and wf.stage(n).fan_out is not None
            for s, k in idx:
                if s == n and self._current(idx, s, k):
                    if loose:
                        deps.append({"stage": s, "key": k, "any": True})
                    else:
                        deps.append(
                            {"stage": s, "key": k, "version": self._current(idx, s, k)["version"]}
                        )
        return deps

    # skill: queued; the skill worker thread runs it when the LLM is available
    def _queue_skill(self, prod, wf, st, idx, note) -> dict:
        prev = self._latest(idx, st.id, "main")
        meta = {"note": note, "previous": prev["id"] if prev and note else None}
        return self.store.add_artifact(
            prod["id"],
            st.id,
            "main",
            "queued",
            deps=self._whole_deps(wf, st, idx),
            note=note,
            meta=meta,
        )

    def skill_messages(
        self, prod: dict, st: Stage, art: dict, extra: dict | None = None
    ) -> list[dict]:
        wf = self._wf(prod)
        idx = self._index(prod["id"])
        ctx = self._ctx(prod, wf, idx)
        skill = self.skills.get(st.skill)
        schema = st.output_schema
        extras = [
            f"## Reference skill: {name}\n\n" + self.skills.get(name).prompt()
            for name in st.extra_skills
            if name in self.skills.skills
        ]
        system = "\n\n".join(
            p
            for p in (
                skill.prompt(),
                *extras,
                st.instructions.strip(),
                "## Output contract\nReply with exactly one JSON object (no prose, no code fence) "
                "that validates against this JSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False),
            )
            if p
        )
        omit = set(st.chunk_by.omit_inputs) if st.chunk_by and extra else set()
        inputs = {n: ctx[n] for n in st.needs if n not in omit}
        for n, fields in st.pick.items():
            if isinstance(inputs.get(n), dict):
                inputs[n] = {f: inputs[n].get(f) for f in fields}
        user = {"production": prod["title"], "settings": prod["settings"], "inputs": inputs}
        prev_id = (art["meta"] or {}).get("previous")
        if prev_id:
            prev = self.store.get_artifact(prev_id)
            user["previous_output"] = prev and prev["data"]
        if art["note"]:
            user["revision_request"] = art["note"]
        if extra:
            user.update(extra)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, indent=1)},
        ]

    def run_skill_job(self, art: dict) -> dict:
        """Run one queued skill job. Raises Park when the LLM is unavailable (the job stays queued)."""
        prod = self._prod(art["production_id"])
        st = self._wf(prod).stage(art["stage"])
        if st.kind == "tool":
            return self._run_tool(prod, st, art)
        if self.llm is None:
            raise Park("no LLM configured")
        self.store.update_artifact(art["id"], status="running")
        stats: dict = {}
        try:
            if st.chunk_by:
                data, stats, raw, violation = self._run_chunks(prod, st, art)
            else:
                messages = self.skill_messages(prod, st, art)
                data, stats, raw, violation = self._llm_json(st, messages, prod)
            if violation is not None:
                art = self.store.update_artifact(
                    art["id"],
                    status="invalid",
                    data={"raw": raw, "violation": violation},
                    meta={**art["meta"], "llm": stats},
                )
                self._emit(prod, st, art, "invalid")
                return art
        except Park:
            self.store.update_artifact(art["id"], status="queued")
            raise
        except Exception as e:  # noqa: BLE001 — record the reason; you decide whether to rerun
            if self._gone(prod["id"]):
                return art  # deleted while the LLM was working
            art = self.store.update_artifact(art["id"], status="failed", note=str(e)[:500])
            self._emit(prod, st, art, "failed")
            return art
        if self._gone(prod["id"]):
            self.log(f"production {prod['id']}: deleted during {st.id}, result dropped")
            return art
        now = self.store.get_artifact(art["id"])
        if now is None or now["status"] != "running":
            self.log(f"production {prod['id']}: {st.id}/{art['key']} was stopped, result dropped")
            return now or art
        status = "current" if self._gate(prod, st) == "auto" else "review"
        art = self.store.update_artifact(
            art["id"], status=status, data=data, meta={**art["meta"], "llm": stats}
        )
        self._emit(prod, st, art, status)
        if status == "current":
            self._supersede(art)
            self.advance(prod["id"])
        return art

    @staticmethod
    def _cap_violation(prod: dict, st: Stage, data: Any) -> str | None:
        if st.cap is None or not isinstance(data, dict):
            return None
        limit = prod["settings"].get(st.cap.setting)
        if not limit:
            return None
        over = [
            f"{el.get('id', i)}={el.get(st.cap.field)}"
            for i, el in enumerate(data.get(st.cap.list) or [])
            if isinstance(el, dict) and float(el.get(st.cap.field) or 0) > float(limit)
        ]
        if not over:
            return None
        return (
            f"{st.cap.list}[].{st.cap.field} must be at most {limit} ({st.cap.setting}): "
            + ", ".join(over[:8])
            + ". Split each one into consecutive elements of at most that length."
        )

    def _llm_json(
        self, st: Stage, messages: list[dict], prod: dict | None = None
    ) -> tuple[Any, dict, str, str | None]:
        """One reply validated against the stage schema (and its cap), with one explicit repair
        chance (policy: repair once, do not change what the task means).
        Returns (data, stats, raw, violation)."""

        def check(text: str) -> Any:
            data = extract_json(text)
            jsonschema.validate(data, st.output_schema)
            cap = self._cap_violation(prod, st, data) if prod else None
            if cap:
                raise jsonschema.ValidationError(cap)
            return data

        text, stats = self.llm(st, messages, st.output_schema)
        try:
            return check(text), stats, text, None
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
        messages = [
            *messages,
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": f"That output is invalid: {msg}. Reply with the corrected JSON only.",
            },
        ]
        text, stats2 = self.llm(st, messages, st.output_schema)
        stats = {**stats, "repair": stats2}
        try:
            return check(text), stats, text, None
        except (json.JSONDecodeError, jsonschema.ValidationError) as e2:
            msg = e2.message if isinstance(e2, jsonschema.ValidationError) else str(e2)
            return None, stats, text, msg

    def _run_chunks(self, prod: dict, st: Stage, art: dict) -> tuple[Any, dict, str, str | None]:
        """chunk_by: one call per upstream item; the `merge` lists are joined in item order."""
        cb = st.chunk_by
        ctx = self._ctx(prod, self._wf(prod), self._index(prod["id"]))
        items = [it for it in lookup(ctx, cb.items) or [] if isinstance(it, dict)]
        minutes = prod["settings"].get(cb.budget_minutes) if cb.budget_minutes else 0
        budget = float(minutes or 0) * 60
        weights = [max(1, len(it.get(cb.weight) or [])) if cb.weight else 1 for it in items]
        merged: list = []
        rest: dict = {}
        all_stats: list = []
        for i, item in enumerate(items):
            if self._gone(prod["id"]):
                raise ProductionError("production deleted")
            chunk = {"index": i + 1, "of": len(items), "elements_so_far": len(merged)}
            if budget:
                chunk["target_seconds"] = round(budget * weights[i] / sum(weights))
            extra = {"item": item, "chunk": chunk}
            if merged:
                extra["previous_element"] = merged[-1]  # continuity across the seam
            data, stats, raw, violation = self._llm_json(
                st, self.skill_messages(prod, st, art, extra), prod
            )
            all_stats.append(stats)
            if violation is not None:
                return None, {"chunks": all_stats}, raw, f"item {i + 1}/{len(items)}: {violation}"
            merged += data.get(cb.merge) or []
            rest = {**data, **rest}  # other fields: the first reply wins
            self.log(f"{st.id}: item {i + 1}/{len(items)} → {len(merged)} {cb.merge}")
        if cb.id_format:
            for n, el in enumerate(merged, 1):
                if isinstance(el, dict):
                    el["id"] = cb.id_format.format(n=n)
        data = {**rest, cb.merge: merged}
        try:
            jsonschema.validate(data, st.output_schema)
        except jsonschema.ValidationError as e:
            return None, {"chunks": all_stats}, json.dumps(data, ensure_ascii=False), e.message
        return data, {"chunks": all_stats}, "", None

    def _run_tool(self, prod: dict, st: Stage, art: dict) -> dict:
        self.store.update_artifact(art["id"], status="running")
        try:
            ctx = self._ctx(prod, self._wf(prod), self._index(prod["id"]))
            data = self.tools[st.tool](prod, st, {n: ctx[n] for n in st.needs})
            if st.output_schema:
                jsonschema.validate(data, st.output_schema)
        except Exception as e:  # noqa: BLE001 — record the reason; you decide whether to rerun
            if self._gone(prod["id"]):
                return art
            art = self.store.update_artifact(art["id"], status="failed", note=str(e)[:500])
            self._emit(prod, st, art, "failed")
            return art
        if self._gone(prod["id"]):
            return art  # deleted while the tool ran
        status = "current" if self._gate(prod, st) == "auto" else "review"
        art = self.store.update_artifact(art["id"], status=status, data=data)
        self._emit(prod, st, art, status)
        if status == "current":
            self._supersede(art)
            self.advance(prod["id"])
        return art

    def _tool_ingest(self, prod: dict, st: Stage, inputs: dict) -> dict:
        from .ingest import Ingest

        def upload_path(uid: str) -> str:
            up = self.store.get_upload(uid)
            if up is None:
                raise FileNotFoundError(f"upload {uid} not found")
            return up["path"]

        work = self.work_dir / prod["id"] / f"{st.id}_{int(time.time())}"
        data_dir = self.store.jobs_dir.parent
        return Ingest(work, upload_path, file_roots=[data_dir])(
            inputs[st.needs[0]], prod["settings"]
        )

    def pending_skill_jobs(self) -> list[dict]:
        jobs = []
        for prod in self.store.productions():
            if prod["state"] == "active":
                wf = self.workflows.get(prod["workflow"])
                jobs += [
                    a
                    for a in self.store.artifacts(prod["id"], None, "queued")
                    if wf and wf.stage(a["stage"]).kind in ("skill", "tool")
                ]
        return sorted(jobs, key=lambda a: a["created"])

    def recover(self) -> int:
        """Process restart: skill jobs that were halfway go back to the queue (LLM calls have no side effects, rerunning is safe)."""
        n = 0
        for prod in self.store.productions():
            wf = self.workflows.get(prod["workflow"])
            for a in self.store.artifacts(prod["id"], None, "running"):
                kind = wf.stage(a["stage"]).kind if wf else None
                if not a["round_id"] and kind in (
                    "skill",
                    "tool",
                ):  # external jobs such as film shots stay running
                    self.store.update_artifact(a["id"], status="queued")
                    n += 1
        return n

    def work(self, stop: threading.Event, idle_s: float = 30.0) -> None:
        """Skill worker thread: runs queued jobs in order; when the LLM is unavailable, waits for the render to end (wake) or a timeout and retries."""
        while not stop.is_set():
            parked = False
            filming = 0
            if self.film is not None:
                try:
                    self.film.tick()
                    filming = len(self._films_waiting())
                # FreeVideo temporarily unreachable: check again next pass
                except Exception as e:  # noqa: BLE001
                    self.log(f"film poll failed: {e}")
            for art in self.pending_skill_jobs():
                try:
                    self.run_skill_job(art)
                except Park as e:
                    self.log(f"skill {art['stage']} parked: {e}")
                    parked = True
                    break
                # one failing job must not stop the whole background thread
                except Exception as e:  # noqa: BLE001
                    self.log(f"job {art['stage']}/{art['key']} failed: {e}")
                    self.store.update_artifact(art["id"], status="failed", note=str(e)[:500])
            idle = min(idle_s, 10.0) if filming else idle_s
            self._wake.wait(idle if parked or not self.pending_skill_jobs() else 0.1)
            self._wake.clear()

    def _films_waiting(self) -> list[dict]:
        out = []
        for prod in self.store.productions():
            if prod["state"] == "active":
                out += [
                    a
                    for a in self.store.artifacts(prod["id"], None, "running")
                    if ((a["meta"] or {}).get("film") or (a["meta"] or {}).get("pending_ref"))
                    and not a["round_id"]
                ]
        return out

    def wake(self) -> None:
        self._wake.set()

    def _item_deps(self, prod, st, idx, ctx, key) -> list[dict]:
        """Dependencies of one item of a fanned-out stage: its entry in the upstream list (by content fingerprint) + the other upstream stages (loosely)."""
        item = dict(self._items(st, ctx)).get(key, {})
        src_stage = st.fan_out.items.split(".")[0]
        cur = self._current(idx, src_stage, "main")
        deps = [
            {
                "stage": src_stage,
                "key": "main",
                "path": st.fan_out.items.split(".", 1)[1],
                "field": st.fan_out.key,
                "item": key,
                "hash": fingerprint(item),
                "version": cur and cur["version"],
            }
        ]
        if st.cast:
            c = self._current(idx, st.cast, "main")
            deps.append({"stage": st.cast, "key": "main", "version": c and c["version"]})
        return deps

    # render: one round of candidates; picking one approves it
    def _start_render(self, prod, st, idx, ctx, key, origin) -> dict:
        items = dict(self._items(st, ctx)) if st.fan_out else {}
        item = items.get(key, {})
        local = {**ctx, "item": item, "key": key}
        deps: list[dict] = []
        if st.fan_out:
            src_stage = st.fan_out.items.split(".")[0]
            cur = self._current(idx, src_stage, "main")
            deps.append(
                {
                    "stage": src_stage,
                    "key": "main",
                    "path": st.fan_out.items.split(".", 1)[1],
                    "field": st.fan_out.key,
                    "item": key,
                    "hash": fingerprint(item),
                    "version": cur and cur["version"],
                }
            )
        else:
            deps = self._whole_deps(self._wf(prod), st, idx)
        ref_stage, ref = self._reference(ctx, item.get("reference"))
        if ref is not None:
            local["ref"] = ref
            cur = self._current(idx, ref_stage, "main")
            deps.append({"stage": ref_stage, "key": "main", "version": cur and cur["version"]})
        spec = st.render
        for alt in st.alternatives:
            v = lookup(local, alt.when.path)
            if (v == alt.when.equals) if alt.when.equals is not None else bool(v):
                # taken as-is (e.g. a casting photo you provided): directly an approved artifact
                if alt.provide:
                    return self._provided(
                        prod, st.id, key, interp(alt.provide, local), deps, "provided by reference"
                    )
                spec = alt.render or spec
                break
        params = {}
        for name, v in spec.params.items():
            v = interp(v, local)
            if isinstance(v, str) and v.startswith("@art:"):
                v, dep = self._art_ref(idx, v)
                deps.append(dep)
            if v not in (None, ""):
                params[name] = v
        tier = interp(spec.tier, local) if spec.tier else None
        n = interp(spec.n, local) if spec.n is not None else None
        rnd = self.studio.start_round(
            spec.template,
            params,
            int(n) if n else None,
            tier or None,
            f"{prod['title']} · {st.title or st.id} · {key}",
            origin=origin,
            request=f"production:{prod['id']}:{st.id}:{key}",
            project_id=prod["project_id"],
            # the tier comes from your settings when you created the production (counts as explicit)
            allow_unverified=True,
        )
        return self.store.artifact_by_round(rnd["id"]) or self._adopt(
            rnd, prod["id"], st.id, key, deps
        )

    @staticmethod
    def _reference(ctx: dict, ref_id: str | None) -> tuple[str | None, dict | None]:
        """An item's reference (a reference image ID in the material pack) → (its stage, the reference image)."""
        if not ref_id:
            return None, None
        for sid, data in ctx.items():
            if isinstance(data, dict) and isinstance(data.get("references"), list):
                for r in data["references"]:
                    if r.get("id") == ref_id:
                        return sid, r
        return None, None

    def _art_ref(self, idx, ref: str) -> tuple[str, dict]:
        """@art:stage/key → the task picked in that artifact's current version (@task:ID) or the file you provided, recorded as a dependency."""
        stage, _, key = ref[len("@art:") :].partition("/")
        cur = self._current(idx, stage, key or "main")
        if cur is None or not (cur.get("task_id") or (cur.get("data") or {}).get("provided")):
            raise ProductionError(f"引用的产物 {stage}/{key} 还没有通过")
        if cur.get("task_id"):
            value = f"@task:{cur['task_id']}"
        else:
            value = next(o["path"] for o in cur["data"]["outputs"])
        return value, {
            "stage": stage,
            "key": key or "main",
            "version": cur["version"],
        }

    def _provided(self, prod, stage, key, path: str, deps: list, note: str) -> dict:
        src = Path(path)
        if not src.is_file():
            raise ProductionError(f"找不到文件 {path}")
        d = self.work_dir / prod["id"] / "provided"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{stage}_{key}_{int(time.time() * 1000)}{src.suffix.lower()}"
        shutil.copy2(src, dest)
        kind = "video" if dest.suffix in (".mp4", ".mov", ".webm") else "image"
        art = self.store.add_artifact(
            prod["id"],
            stage,
            key,
            "current",
            deps=deps,
            note=note,
            data={
                "provided": True,
                "outputs": [{"kind": kind, "file": dest.name, "path": str(dest)}],
            },
        )
        self._supersede(art)
        return art

    def provide(
        self, production_id: str, stage_id: str, key: str, path: str, note: str | None = None
    ) -> dict:
        """Use your own image / video as a render item (e.g. a casting photo), counted as approved; downstream goes stale by dependency as usual."""
        with self._lock:
            prod = self._prod(production_id)
            st = self._wf(prod).stage(stage_id)
            if st.kind not in ("render", "assemble"):
                raise ProductionError("只有渲染类阶段的条目可以用你自己的文件替换")
            idx = self._index(production_id)
            latest = self._latest(idx, stage_id, key)
            if latest and latest["status"] in RUNNING:
                raise ProductionError(f"{stage_id}/{key} 正在生成，先等它结束或打回")
            art = self._provided(prod, stage_id, key, path, [], note or "provided by you")
            self.advance(production_id)
            return art

    def _adopt(self, rnd, production_id, stage, key, deps) -> dict:
        return self.store.add_artifact(
            production_id, stage, key, "running", deps=deps, round_id=rnd["id"]
        )

    # assemble: edit timeline + approved shots → FFmpeg
    def _start_assemble(self, prod, wf, st, idx, ctx, origin) -> dict:
        tl_art = self._current(idx, st.timeline)
        tl = tl_art["data"]
        clips_stage = wf.stage(st.clips)
        clip_arts = {k: self._current(idx, st.clips, k) for (s, k) in idx if s == st.clips}
        timeline = self.materialize(tl, clip_arts, prod["settings"])
        d = self.work_dir / prod["id"]
        d.mkdir(parents=True, exist_ok=True)
        path = (
            d
            / f"timeline_v{(self._latest(idx, st.id, 'main') or {'version': 0})['version'] + 1}.json"
        )
        path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2))
        deps = [{"stage": st.timeline, "key": "main", "version": tl_art["version"]}]
        deps += [
            {"stage": clips_stage.id, "key": c["shot"], "version": clip_arts[c["shot"]]["version"]}
            for c in tl["clips"]
        ]
        rnd = self.studio.start_round(
            "ffmpeg-assemble",
            {"timeline": str(path), "subtitles": bool(prod["settings"].get("subtitles", True))},
            1,
            None,
            f"{prod['title']} · {st.title or st.id}",
            origin=origin,
            request=f"production:{prod['id']}:{st.id}:main",
            project_id=prod["project_id"],
        )
        return self.store.artifact_by_round(rnd["id"]) or self._adopt(
            rnd, prod["id"], st.id, "main", deps
        )

    @staticmethod
    def materialize(tl: dict, clip_arts: dict[str, dict | None], settings: dict) -> dict:
        """The edit skill's timeline (by shot ID) → an FFmpeg timeline (file paths + absolute subtitle times)."""
        clips, subs, t = [], [], 0.0
        trans = tl.get("transition") or {}
        overlap = (
            float(trans.get("seconds") or 0) if trans.get("type") not in (None, "cut") else 0.0
        )
        for i, c in enumerate(tl["clips"]):
            art = clip_arts.get(c["shot"])
            if art is None:
                raise ProductionError(f"镜头 {c['shot']} 还没有通过的视频")
            video = next((o for o in art["data"]["outputs"] if o["kind"] == "video"), None)
            if video is None:
                raise ProductionError(f"镜头 {c['shot']} 的产物里没有视频")
            full = probe_seconds(video["path"])
            start = float(c.get("in") or 0)
            end = min(float(c.get("out") or full), full)
            clips.append({"file": video["path"], "in": start, "out": end, "shot": c["shot"]})
            if c.get("subtitle"):
                subs.append(
                    {"start": round(t, 3), "end": round(t + end - start, 3), "text": c["subtitle"]}
                )
            t += end - start - (overlap if i < len(tl["clips"]) - 1 else 0)
        out = {
            "width": settings.get("width", 1280),
            "height": settings.get("height", 720),
            "fps": settings.get("fps", 24),
            "clips": clips,
            "subtitles": subs,
        }
        if overlap:
            out["transition"] = {"type": trans["type"], "seconds": overlap}
        return out

    # ---- round callbacks: follow rounds derived by redo / refine; advance after a decision ----
    def _on_round_created(self, rnd: dict) -> None:
        if not rnd.get("parent_round") or self.store.artifact_by_round(rnd["id"]):
            return
        if self.film is not None and self.film.intercept_round(rnd):
            return  # redo / reject of a FreeVideo take: turned into a FreeVideo reshoot
        parent = self.store.artifact_by_round(rnd["parent_round"])
        if parent is None:
            return
        self._adopt(rnd, parent["production_id"], parent["stage"], parent["key"], parent["deps"])

    def _on_round_decided(self, rnd: dict) -> None:
        art = self.store.artifact_by_round(rnd["id"])
        if art is None:
            return
        art = self._sync(art)
        prod = self._prod(art["production_id"])
        if art["status"] == "current":
            if self.film is not None:
                # FreeVideo: pick this take; later continuations continue from it
                self.film.on_chosen(art, rnd)
            self._supersede(art)
            self.advance(prod["id"])
            return
        d = rnd.get("decision") or {}
        if d.get("action") == "reject":
            self._cascade(art, f"{art['stage']}/{art['key']} was rejected")
        if (
            d.get("action") == "reject"
            and not d.get("note")
            and prod["state"] == "active"
            and self.rules(prod, art["stage"])["on_reject"] == "rerender"
        ):
            # items in a production cannot stay empty: a reject without a note = rerun with new
            # seeds (with a note, the planner changes parameters and derives a new round)
            self.log(f"production {prod['id']}: {art['stage']}/{art['key']} rejected → re-render")
            try:
                self.run_stage(prod["id"], art["stage"], [art["key"]], origin="production")
            except ProductionError as e:
                self.log(f"production {prod['id']}: re-render of {art['key']} not started: {e}")

    def on_round_ready(self, rnd: dict) -> None:
        """A round of candidates finished: auto-approved stages (gate: auto) pick the first successful candidate directly."""
        art = self.store.artifact_by_round(rnd["id"])
        if art is None:
            return
        prod = self._prod(art["production_id"])
        st = self._wf(prod).stage(art["stage"])
        art = self._sync(art)
        if (
            self._gate(prod, st) == "auto"
            and art["status"] == "review"
            and not (art["meta"] or {}).get("stopped")
        ):
            ok = next((t for t in self.store.round_tasks(rnd["id"]) if t["outputs"]), None)
            if ok is None:  # nothing usable: leave it for you
                self._emit(prod, st, art, art["status"])
                return
            self.studio.select(rnd["id"], ok["id"], note="auto (gate: auto)", origin="production")
        else:
            self._emit(prod, st, art, art["status"])

    def _supersede(self, art: dict) -> None:
        for a in self.store.artifacts(art["production_id"], art["stage"], "current"):
            if a["key"] == art["key"] and a["id"] != art["id"] and a["version"] < art["version"]:
                self.store.update_artifact(a["id"], status="superseded")

    # ---- review (skill stages; render stages are reviewed on rounds) ----
    def review(
        self,
        production_id: str,
        stage_id: str,
        action: Literal["approve", "redo", "reject", "keep"],
        note: str | None = None,
        data: Any = None,
        key: str = "main",
        origin: str = "web",
    ) -> dict:
        with self._lock:
            prod = self._prod(production_id)
            st = self._wf(prod).stage(stage_id)
            idx = self._index(production_id)
            art = self._latest(idx, stage_id, key)
            if art is None:
                raise ProductionError(f"{stage_id}/{key} 还没有产物")
            if art["round_id"]:
                raise ProductionError("渲染阶段在候选轮上审阅（选中 / 重做 / 微调 / 打回）")
            # upstream changed but this version still applies: re-stamp against the current
            # upstream, content unchanged
            if action == "keep":
                cur = self._current(idx, stage_id, key)
                if cur is None:
                    raise ProductionError(f"{stage_id} 没有已通过的版本可保留")
                art = self.store.update_artifact(
                    cur["id"], deps=self._whole_deps(self._wf(prod), st, idx), note=note or "kept"
                )
                self.advance(production_id)
                return art
            if action == "approve":
                if art["status"] not in ("review", "invalid") and data is None:
                    raise ProductionError(f"{stage_id} 当前状态 {art['status']}，没有待审内容")
                # content you edited by hand: validated and approved as a new version
                if data is not None:
                    jsonschema.validate(data, st.output_schema)
                    art = self.store.add_artifact(
                        production_id,
                        stage_id,
                        key,
                        "current",
                        data=data,
                        deps=art["deps"],
                        note=note or "edited by hand",
                    )
                else:
                    if art["status"] == "invalid":
                        raise ProductionError("输出未通过 schema 校验；请手改后提交或重做")
                    art = self.store.update_artifact(art["id"], status="current", note=note)
                self._supersede(art)
                # into the current session: a message without one resurfaces in a "history
                # session" after you delete sessions
                self.studio.log(origin, "in", f"approved {stage_id}", prod["project_id"])
                self.advance(production_id)
                return art
            if action == "redo":
                # the approved version stays valid until a new one is approved (downstream
                # unaffected); the pending / invalid one is discarded
                if art["status"] != "current":
                    self.store.update_artifact(art["id"], status="rejected", note=note)
                return self.run_stage(production_id, stage_id, [key], note or "redo", origin)[0]
            self.store.update_artifact(art["id"], status="rejected", note=note)
            self._cascade(art, f"{stage_id}/{key} was rejected")
            return self.store.get_artifact(art["id"])

    def pending_reviews(self, project_id: str | None = None) -> list[dict]:
        """Planning stages waiting for you in all active productions (pending review / failed validation / upstream changed). Render stages are reviewed on rounds."""
        out = []
        for prod in self.store.productions(project_id):
            if prod["state"] != "active":
                continue
            plan = self.plan(prod["id"])
            for s in plan["stages"]:
                if s["kind"] not in ("skill", "tool") or s["state"] == "blocked":
                    continue
                for it in s["items"]:
                    if it["state"] in ("review", "invalid", "stale"):
                        art = self.store.get_artifact(it["artifact_id"])
                        out.append(
                            {
                                "production_id": prod["id"],
                                "production": prod["title"],
                                "project_id": prod["project_id"],
                                "stage": s["id"],
                                "title": s["title"],
                                "key": it["key"],
                                "state": it["state"],
                                "version": it["version"],
                                "data": art["data"],
                                "note": art["note"],
                                "updated": art["updated"],
                            }
                        )
        return out

    # ---- advance: after a stage is approved, start the next ready stages automatically (confirm
    # stages wait for you to press start) ----
    def advance(self, production_id: str) -> list[str]:
        prod = self._prod(production_id)
        if not prod["settings"].get("auto_advance", True) or prod["state"] != "active":
            return []
        started = []
        plan = self.plan(production_id)
        for s in plan["stages"]:
            if s["state"] != "ready" or s["confirm"] or s["kind"] == "input":
                continue
            auto = ("missing",) if s["kind"] == "skill" else ("missing", "stale")
            fresh = [i["key"] for i in s["items"] if i["state"] in auto]
            if not fresh:
                continue  # failed / rejected items wait for your decision, not rerun automatically
            try:
                self.run_stage(production_id, s["id"], fresh, origin="production")
                started.append(s["id"])
            except Exception as e:  # noqa: BLE001 — recorded; the plan will show it as ready
                self.log(f"production {production_id}: auto-start {s['id']} failed: {e}")
        return started

    def _emit(self, prod: dict, st: Stage, art: dict, status: str) -> None:
        ev = {
            "production": prod,
            "stage": st.id,
            "title": st.title or st.id,
            "key": art["key"],
            "status": status,
            "artifact": art,
        }
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception as e:  # noqa: BLE001
                self.log(f"production listener failed: {e}")
