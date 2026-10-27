"""制片流水线（阶段 3）：声明式工作流 + 带版本与过期传播的产物库 + 权威计划 + 阶段执行。

小说 → 成片只是 `workflows/` 里的一套 YAML；换一套 YAML（广告片、MV……）不改代码。

- 工作流规格：阶段 DAG。阶段类型 input（用户给的原文）/ skill（LLM 按 SKILL.md 产出 JSON，
  Schema 校验）/ render（按模板出图出视频，可按上游列表展开）/ assemble（把剪辑时间线交给 FFmpeg）。
- 产物（artifact）：每个 (阶段, key) 有多个版本；记录生成时依赖的上游产物（版本或条目指纹）。
  上游换了版本 → 依赖它的下游**算出来**是 stale，不需要级联写库；改一个角色只影响引用它的镜头。
- 计划（plan）：由规格 + 产物确定性算出每阶段状态与 next_action。控制台、Telegram、agent、
  Claude Code 看到的是同一份计划；agent 不自建状态机。
- 渲染阶段复用现有“轮 / 候选 / 审阅”：选中即通过；重做、微调派生的新轮自动跟随到同一产物。
- skill 阶段的 LLM 调用由调用方注入（daemon 按资源闸门与回退链选择后端）；资源不够时排队，
  渲染结束后再跑。
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


# ---------------------------------------------------------------- 规格
class FanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: str  # 上游数据里的列表路径，例如 "bible.characters"
    key: str = "id"


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str
    params: dict[str, Any] = {}
    tier: str | None = None  # 可写 "{settings.video_tier}"
    n: int | str | None = None


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # 上下文里的路径，例如 ref.use
    equals: Any = None  # 省略：路径的值为真即可


class Alternative(BaseModel):
    """渲染条目的条件分支（第一条匹配的生效）：直接采用一张图（provide），或换一种渲染。"""

    model_config = ConfigDict(extra="forbid")

    when: When
    provide: str | None = None  # 图片路径表达式，例如 "{ref.path}"：原样作为已通过的产物
    render: RenderSpec | None = None


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    kind: Literal["input", "tool", "skill", "render", "assemble", "film"]
    cast: str | None = None  # film：角色卡来自哪个阶段（{"characters": {key: card id}}）
    tool: str | None = None  # tool 阶段：编排器里注册的确定性工具（例如 ingest）
    needs: list[str] = []
    skill: str | None = None
    extra_skills: list[str] = []  # 一并读的参考 skill（例如 MiniMax 官方 h3-prompt-writing）；没装就跳过
    llm: str | None = None  # 指定后端（如 claude_code）；留空用制片设置 / 主后端
    instructions: str = ""
    output_schema: dict | str | None = Field(None, alias="schema")
    fan_out: FanOut | None = None
    render: RenderSpec | None = None
    alternatives: list[Alternative] = []
    timeline: str | None = None  # assemble：时间线来自哪个阶段
    clips: str | None = None  # assemble：片段来自哪个阶段
    gate: Literal["review", "auto"] = "review"
    confirm: bool = False  # True：上一阶段通过后不自动开跑（例如视频，耗时以小时计），等你点开始


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    description: str = ""
    settings: dict[str, Any] = {}
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
            for st in raw.get("stages", []):  # schema 可写文件名：workflows/schemas/<name>.json
                sch = st.get("schema")
                if isinstance(sch, str):
                    st["schema"] = json.loads((root / "schemas" / f"{sch}.json").read_text())
            wf = Workflow(**raw)
            out[wf.name] = wf
    return out


# ---------------------------------------------------------------- 表达式
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
    """字符串里的 {a.b.c} 用上下文替换；整个字符串就是一个引用时保留原类型。"""
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
    """模型回复里取出 JSON（容忍 ```json 围栏与前后说明文字）。"""
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
    """LLM 现在不能跑（资源不够且没有可用回退）：skill 作业留在队列里，资源释放后再试。"""


class ProductionError(RuntimeError):
    pass


# ---------------------------------------------------------------- 引擎
class Production:
    """制片引擎。`llm(stage, messages, schema) -> (text, stats)`；不能跑时抛 Park。"""

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
        # tool 阶段的确定性工具：fn(production, stage, inputs) -> data；可注册更多
        self.tools: dict[str, Callable] = {"ingest": self._tool_ingest, **(tools or {})}
        self.film = None  # film 阶段（FreeVideo 多镜头电影）；没有 FreeVideo 时这类阶段不能开跑
        if freevideo is not None:
            from .film import FilmDriver

            self.film = FilmDriver(self, freevideo)
            self.tools.setdefault("freevideo_cards", self.film.cards_tool)
        self.workflows, self.llm, self.log = workflows, llm, log
        self.work_dir = work_dir or (self.store.jobs_dir.parent / "productions")
        self.listeners: list[Callable[[dict], None]] = []  # 阶段进入待审阅 / 失败 / 完成 → 通知
        self.stop_running: Callable[[str], dict | None] = lambda pid: None  # 由 daemon 注入：中断该制片正在跑的任务
        self._lock = threading.RLock()
        self._wake = threading.Event()
        studio.round_listeners.append(self._on_round_created)
        studio.decision_listeners.append(self._on_round_decided)

    # ---- 创建 ----
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
        """改制片设置（例如对白语言、视频档位）。不会自动重跑已通过的产物——下次开跑的条目按新设置。"""
        prod = self._prod(production_id)
        return self.store.update_production(production_id, settings={**prod["settings"], **changes})

    # ---- 暂停 / 继续 ----
    def task_production(self, task: dict) -> str | None:
        art = self.store.artifact_by_round(task["round_id"]) if task.get("round_id") else None
        return art and art["production_id"]

    def holds(self, task: dict) -> bool:
        """Worker 取任务前问一句：所属制片暂停了就先不执行（队列保留，继续后照常跑）。"""
        if task.get("template") == "freevideo-take":
            return True  # FreeVideo 的 take 由 FreeVideo 自己拍，不经 Worker
        pid = self.task_production(task)
        if pid is None:
            return False
        prod = self.store.get_production(pid)
        return bool(prod and prod["state"] == "paused")

    def pause(self, production_id: str, stop_running: bool = False) -> dict:
        """暂停：不再开跑新条目、排着的渲染与 LLM 作业都先不执行；可选立即中断正在跑的那个。"""
        prod = self._prod(production_id)
        if prod["state"] not in ("active", "done", "paused"):
            raise ProductionError(f"制片状态 {prod['state']}，不能暂停")
        prod = self.store.update_production(production_id, state="paused")
        stopped = self.stop_running(production_id) if stop_running else None
        if stop_running and self.film is not None:
            self.film.cancel(production_id)  # 取消还没拍完的 take（拍好的保留）
        self.log(f"production {production_id}: paused" + (f", interrupted {stopped['id']}" if stopped else ""))
        return {**prod, "interrupted": stopped and stopped["id"]}

    def resume(self, production_id: str) -> dict:
        prod = self._prod(production_id)
        if prod["state"] != "paused":
            raise ProductionError("这个制片没有暂停")
        prod = self.store.update_production(production_id, state="active")
        plan = self.plan(production_id)  # 可能已经全部完成
        for s in plan["stages"]:  # 暂停时被中断而失败的渲染条目：继续时重跑一次
            failed = [i["key"] for i in s["items"] if i["state"] == "failed"]
            if s["kind"] == "render" and failed and s["state"] != "blocked":
                self.run_stage(production_id, s["id"], failed, "resumed after pause", "production")
        self.advance(production_id)
        self._wake.set()
        self.log(f"production {production_id}: resumed")
        return self.store.get_production(production_id)

    def _wf(self, prod: dict) -> Workflow:
        return self.workflows[prod["workflow"]]

    def _prod(self, production_id: str) -> dict:
        prod = self.store.get_production(production_id)
        if prod is None:
            raise ProductionError(f"找不到制片 {production_id}")
        return prod

    # ---- 产物索引 ----
    def _sync(self, art: dict) -> dict:
        """渲染产物的状态从它的轮推导（审阅可能在控制台、Telegram、agent 任一处发生，也可能被重开）。"""
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
        """阶段的数据：单产物阶段给 data；展开的阶段给 {key: data}。"""
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
            # speech_hint：由 language 派生，供视频提示词使用（H3 会生成人声）
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

    # ---- 计划（权威、确定性） ----
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
                elif st.fan_out and not items:  # 上游没有条目（例如没有人物）：视为完成
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
                        "gate": st.gate,
                        "confirm": st.confirm,
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
                    # 上游变了：保留（确认仍然适用）还是重跑，由你决定——重跑会改动下游
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
            elif not done and prod["state"] == "done":  # 完成后又重跑 / 换图 / 加了阶段：回到进行中
                prod = self.store.update_production(production_id, state="active")
                self._wake.set()  # 它排着的作业现在可以执行了
            return {
                "production": prod,
                "workflow": {"name": wf.name, "title": wf.title},
                "stages": stages,
                "next_action": nxt,
                "done": done,
            }

    # ---- 执行 ----
    def run_stage(
        self,
        production_id: str,
        stage_id: str,
        keys: list[str] | None = None,
        note: str | None = None,
        origin: str = "web",
    ) -> list[dict]:
        """开跑一个阶段（默认只跑缺失 / 过期 / 失败的条目）。返回新建的产物。"""
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
            busy = {i["key"] for i in ps["items"] if i["state"] in ("running", "review")}
            if keys is None:
                keys = [
                    i["key"]
                    for i in ps["items"]
                    if i["state"] not in ("current", "running", "review")
                ]
            elif busy & set(keys):
                raise ProductionError(
                    f"这些条目正在生成或待审阅：{', '.join(sorted(busy & set(keys)))}"
                )
            idx = self._index(production_id)
            ctx = self._ctx(prod, wf, idx)
            made = []
            if st.kind == "film":
                if self.film is None:
                    raise ProductionError("film 阶段需要 FreeVideo（freevideo_agent 未启用）")
                if keys:
                    made = self.film.start(
                        prod, st, idx, ctx, keys, note,
                        lambda k: self._item_deps(prod, st, idx, ctx, k),
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
            if any(m["status"] == "current" for m in made):  # 有条目直接采用了你的图：可能已能推进
                self.advance(production_id)
            return made

    def _whole_deps(self, wf: Workflow, st: Stage, idx) -> list[dict]:
        deps = []
        for n in st.needs:
            # 规划阶段读的是展开渲染阶段的“有哪些已通过的条目”，不是像素：换一张定妆图不让分镜过期
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

    # skill：排队，由 skill 工作线程在 LLM 可用时执行
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

    def skill_messages(self, prod: dict, st: Stage, art: dict) -> list[dict]:
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
        inputs = {n: ctx[n] for n in st.needs}
        user = {"production": prod["title"], "settings": prod["settings"], "inputs": inputs}
        prev_id = (art["meta"] or {}).get("previous")
        if prev_id:
            prev = self.store.get_artifact(prev_id)
            user["previous_output"] = prev and prev["data"]
        if art["note"]:
            user["revision_request"] = art["note"]
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, indent=1)},
        ]

    def run_skill_job(self, art: dict) -> dict:
        """执行一个排队的 skill 作业。LLM 不可用时抛 Park（作业仍在队列里）。"""
        prod = self._prod(art["production_id"])
        st = self._wf(prod).stage(art["stage"])
        if st.kind == "tool":
            return self._run_tool(prod, st, art)
        if self.llm is None:
            raise Park("no LLM configured")
        messages = self.skill_messages(prod, st, art)
        self.store.update_artifact(art["id"], status="running")
        stats: dict = {}
        try:
            text, stats = self.llm(st, messages, st.output_schema)
            try:
                data = extract_json(text)
                jsonschema.validate(data, st.output_schema)
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                # 一次显式的修复机会：把违约原样告诉模型（策略：只修一次，不改任务语义）
                msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": f"That output is invalid: {msg}. Reply with the corrected JSON only.",
                    },
                ]
                text, stats2 = self.llm(st, messages, st.output_schema)
                stats = {**stats, "repair": stats2}
                try:
                    data = extract_json(text)
                    jsonschema.validate(data, st.output_schema)
                except (json.JSONDecodeError, jsonschema.ValidationError) as e2:
                    msg = e2.message if isinstance(e2, jsonschema.ValidationError) else str(e2)
                    art = self.store.update_artifact(
                        art["id"],
                        status="invalid",
                        data={"raw": text, "violation": msg},
                        meta={**art["meta"], "llm": stats},
                    )
                    self._emit(prod, st, art, "invalid")
                    return art
        except Park:
            self.store.update_artifact(art["id"], status="queued")
            raise
        except Exception as e:  # noqa: BLE001 — 记下原因，等你决定重跑
            art = self.store.update_artifact(art["id"], status="failed", note=str(e)[:500])
            self._emit(prod, st, art, "failed")
            return art
        status = "current" if st.gate == "auto" else "review"
        art = self.store.update_artifact(
            art["id"], status=status, data=data, meta={**art["meta"], "llm": stats}
        )
        self._emit(prod, st, art, status)
        if status == "current":
            self._supersede(art)
            self.advance(prod["id"])
        return art

    def _run_tool(self, prod: dict, st: Stage, art: dict) -> dict:
        self.store.update_artifact(art["id"], status="running")
        try:
            ctx = self._ctx(prod, self._wf(prod), self._index(prod["id"]))
            data = self.tools[st.tool](prod, st, {n: ctx[n] for n in st.needs})
            if st.output_schema:
                jsonschema.validate(data, st.output_schema)
        except Exception as e:  # noqa: BLE001 — 记下原因，等你决定重跑
            art = self.store.update_artifact(art["id"], status="failed", note=str(e)[:500])
            self._emit(prod, st, art, "failed")
            return art
        status = "current" if st.gate == "auto" else "review"
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
        return Ingest(work, upload_path)(inputs[st.needs[0]], prod["settings"])

    def pending_skill_jobs(self) -> list[dict]:
        jobs = []
        for prod in self.store.productions():
            if prod["state"] == "active":
                jobs += self.store.artifacts(prod["id"], None, "queued")
        return sorted(jobs, key=lambda a: a["created"])

    def recover(self) -> int:
        """进程重启：跑到一半的 skill 作业回到队列（LLM 调用无副作用，重跑安全）。"""
        n = 0
        for prod in self.store.productions():
            for a in self.store.artifacts(prod["id"], None, "running"):
                if not a["round_id"]:
                    self.store.update_artifact(a["id"], status="queued")
                    n += 1
        return n

    def work(self, stop: threading.Event, idle_s: float = 30.0) -> None:
        """skill 工作线程：依次执行排队作业；LLM 不可用时等渲染结束（wake）或超时再试。"""
        while not stop.is_set():
            parked = False
            filming = 0
            if self.film is not None:
                try:
                    self.film.tick()
                    filming = len(self._films_waiting())
                except Exception as e:  # noqa: BLE001 — FreeVideo 暂时不可达：下轮再看
                    self.log(f"film poll failed: {e}")
            for art in self.pending_skill_jobs():
                try:
                    self.run_skill_job(art)
                except Park as e:
                    self.log(f"skill {art['stage']} parked: {e}")
                    parked = True
                    break
            idle = min(idle_s, 10.0) if filming else idle_s
            self._wake.wait(idle if parked or not self.pending_skill_jobs() else 0.1)
            self._wake.clear()

    def _films_waiting(self) -> list[dict]:
        out = []
        for prod in self.store.productions():
            if prod["state"] == "active":
                out += [a for a in self.store.artifacts(prod["id"], None, "running")
                        if ((a["meta"] or {}).get("film") or (a["meta"] or {}).get("pending_ref"))
                        and not a["round_id"]]
        return out

    def wake(self) -> None:
        self._wake.set()

    def _item_deps(self, prod, st, idx, ctx, key) -> list[dict]:
        """展开阶段某个条目的依赖：它在上游列表里的那一项（按内容指纹）+ 其余上游阶段（宽松）。"""
        item = dict(self._items(st, ctx)).get(key, {})
        src_stage = st.fan_out.items.split(".")[0]
        cur = self._current(idx, src_stage, "main")
        deps = [{
            "stage": src_stage, "key": "main", "path": st.fan_out.items.split(".", 1)[1],
            "field": st.fan_out.key, "item": key, "hash": fingerprint(item),
            "version": cur and cur["version"],
        }]
        if st.cast:
            c = self._current(idx, st.cast, "main")
            deps.append({"stage": st.cast, "key": "main", "version": c and c["version"]})
        return deps

    # render：一轮候选，选中即通过
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
                if alt.provide:  # 原样采用（例如你提供的定妆照）：直接作为已通过的产物
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
            allow_unverified=True,  # 档位来自你创建制片时的设置（视为显式指定）
        )
        return self.store.artifact_by_round(rnd["id"]) or self._adopt(
            rnd, prod["id"], st.id, key, deps
        )

    @staticmethod
    def _reference(ctx: dict, ref_id: str | None) -> tuple[str | None, dict | None]:
        """条目上的 reference（素材包里的参考图 ID）→ (所在阶段, 参考图)。"""
        if not ref_id:
            return None, None
        for sid, data in ctx.items():
            if isinstance(data, dict) and isinstance(data.get("references"), list):
                for r in data["references"]:
                    if r.get("id") == ref_id:
                        return sid, r
        return None, None

    def _art_ref(self, idx, ref: str) -> tuple[str, dict]:
        """@art:阶段/key → 该产物当前版本选中的任务（@task:ID）或你提供的文件，并记作依赖。"""
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
        """用你自己的图片 / 视频作为某个渲染条目（例如定妆照），视为已通过；下游照常按依赖过期。"""
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

    # assemble：剪辑时间线 + 已通过的镜头 → FFmpeg
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
        """剪辑 skill 的时间线（按镜头 ID）→ FFmpeg 时间线（文件路径 + 绝对字幕时间）。"""
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

    # ---- 轮的回调：跟随重做 / 微调派生的轮；决定后推进 ----
    def _on_round_created(self, rnd: dict) -> None:
        if not rnd.get("parent_round") or self.store.artifact_by_round(rnd["id"]):
            return
        if self.film is not None and self.film.intercept_round(rnd):
            return  # FreeVideo take 的重做 / 打回：转成 FreeVideo reshoot
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
                self.film.on_chosen(art, rnd)  # FreeVideo：选中这条，后面的续拍从它接着拍
            self._supersede(art)
            self.advance(prod["id"])
            return
        d = rnd.get("decision") or {}
        if d.get("action") == "reject" and not d.get("note") and prod["state"] == "active":
            # 制片里的条目不能空着：不带意见的打回 = 换种子重出（带意见的由规划器改参数后派生新轮）
            self.log(f"production {prod['id']}: {art['stage']}/{art['key']} rejected → re-render")
            try:
                self.run_stage(prod["id"], art["stage"], [art["key"]], origin="production")
            except ProductionError as e:
                self.log(f"production {prod['id']}: re-render of {art['key']} not started: {e}")

    def on_round_ready(self, rnd: dict) -> None:
        """一轮候选出完：自动通过的阶段（gate: auto）直接选第一个成功的候选。"""
        art = self.store.artifact_by_round(rnd["id"])
        if art is None:
            return
        prod = self._prod(art["production_id"])
        st = self._wf(prod).stage(art["stage"])
        art = self._sync(art)
        if st.gate == "auto" and art["status"] == "review":
            ok = next(t for t in self.store.round_tasks(rnd["id"]) if t["outputs"])
            self.studio.select(rnd["id"], ok["id"], note="auto (gate: auto)", origin="production")
        else:
            self._emit(prod, st, art, art["status"])

    def _supersede(self, art: dict) -> None:
        for a in self.store.artifacts(art["production_id"], art["stage"], "current"):
            if a["key"] == art["key"] and a["id"] != art["id"] and a["version"] < art["version"]:
                self.store.update_artifact(a["id"], status="superseded")

    # ---- 审核（skill 阶段；渲染阶段在轮上审） ----
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
            if action == "keep":  # 上游变了但这份仍然适用：按当前上游重新盖章，不改内容
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
                if data is not None:  # 你手改过的内容：校验后作为新版本通过
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
                self.store.log_message(origin, "in", f"approved {stage_id}", prod["project_id"])
                self.advance(production_id)
                return art
            if action == "redo":
                # 已通过的版本在新版本通过前继续有效（下游不受影响）；待审 / 无效的那份作废
                if art["status"] != "current":
                    self.store.update_artifact(art["id"], status="rejected", note=note)
                return self.run_stage(production_id, stage_id, [key], note or "redo", origin)[0]
            self.store.update_artifact(art["id"], status="rejected", note=note)
            return self.store.get_artifact(art["id"])

    def pending_reviews(self, project_id: str | None = None) -> list[dict]:
        """所有进行中制片里等你处理的规划阶段（待审核 / 未通过校验 / 上游变了）。渲染阶段在轮上审。"""
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

    # ---- 推进：上一阶段通过后，自动开跑下一批就绪阶段（confirm 的阶段等你点开始） ----
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
                continue  # 失败 / 打回的条目等你决定，不自动重跑
            try:
                self.run_stage(production_id, s["id"], fresh, origin="production")
                started.append(s["id"])
            except Exception as e:  # noqa: BLE001 — 记下来，计划里会显示为就绪
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
