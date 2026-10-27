"""W2：在 ComfyUI 里改完整 workflow，再导回 aco_lw（设计稿 L4 受控探索）。

生命周期：导入（API 格式）→ 与基础模板对比 + /object_info 校验 → 候选变体 → 最低档试跑
→ 用户确认后晋升为正式模板（data/templates/，所有项目可用）；或被否决。
候选变体只能手动调用，规划器看不到；晋升前不会进入任何自动流程。
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

import yaml

from .render import TIER_KEYS, check_graph
from .studio import RUNNING_STATES, Studio, StudioError

MAX_BYTES = 2 * 2**20
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def is_api_graph(g) -> bool:
    return (
        isinstance(g, dict)
        and bool(g)
        and all(
            isinstance(v, dict) and "class_type" in v and isinstance(v.get("inputs"), dict)
            for v in g.values()
        )
    )


def diff_graph(base: dict, new: dict) -> dict:
    """节点增删、类型变化、参数与连线变化（按节点 ID 对齐；ComfyUI 导入 / 导出 API 会保留 ID）。"""
    added = [{"id": k, "class": v["class_type"]} for k, v in new.items() if k not in base]
    removed = [{"id": k, "class": v["class_type"]} for k, v in base.items() if k not in new]
    retyped, changed, rewired = [], [], []
    for k in base.keys() & new.keys():
        a, b = base[k], new[k]
        if a["class_type"] != b["class_type"]:
            retyped.append({"id": k, "from": a["class_type"], "to": b["class_type"]})
            continue
        for f in sorted(a["inputs"].keys() | b["inputs"].keys()):
            va, vb = a["inputs"].get(f), b["inputs"].get(f)
            if va == vb:
                continue
            entry = {"id": k, "class": a["class_type"], "field": f, "from": va, "to": vb}
            (rewired if isinstance(va, list) or isinstance(vb, list) else changed).append(entry)
    return {
        "added": added,
        "removed": removed,
        "retyped": retyped,
        "changed": changed,
        "rewired": rewired,
    }


def tier_cost(values: dict) -> float:
    return values.get("width", 1) * values.get("height", 1) * values.get("seconds", 1)


class Workshop:
    def __init__(self, studio: Studio):
        self.studio = studio
        self.orch = studio.orch
        self.store = studio.store
        self.dir = self.orch.cfg.data_dir / "variants"
        self.user_dir = self.orch.cfg.data_dir / "templates"

    # ---- 读 ----
    def _meta_path(self, vid: str) -> Path:
        return self.dir / vid / "meta.json"

    def get(self, vid: str) -> dict:
        p = self._meta_path(vid)
        if not p.exists():
            raise StudioError(f"variant {vid} not found")
        return json.loads(p.read_text())

    def _save(self, meta: dict) -> dict:
        meta["updated"] = time.time()
        self._meta_path(meta["id"]).write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return meta

    def list(self, project_id: str | None = None) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for d in sorted(self.dir.iterdir(), reverse=True):
            if (d / "meta.json").exists():
                m = self.refresh(d.name)
                if project_id is None or m["project_id"] == project_id:
                    out.append(m)
        return out

    def export(self, task_id: str) -> tuple[str, bytes]:
        """任务实际提交的 API workflow，用于拖进 ComfyUI 画布编辑。"""
        task = self.store.get(task_id)
        if task is None:
            raise StudioError(f"task {task_id} not found")
        p = self.store.task_dir(task_id) / "prompt.json"
        if not p.exists():
            raise StudioError("this task has no submitted workflow yet")
        return f"aco_lw-{task['template']}-{task_id}.json", p.read_bytes()

    # ---- 导入 ----
    def import_graph(
        self,
        data: bytes,
        base: str,
        project_id: str | None = None,
        task_id: str | None = None,
        note: str | None = None,
    ) -> dict:
        if len(data) > MAX_BYTES:
            raise StudioError("workflow file too large (> 2 MB)")
        try:
            graph = json.loads(data)
        except json.JSONDecodeError as e:
            raise StudioError(f"not valid JSON: {e}") from e
        if not is_api_graph(graph):
            raise StudioError("not an API-format workflow — in ComfyUI use Workflow → Export (API)")
        reg = self.orch.registry
        t = reg.template(base)
        while t.origin == "variant":  # 变体上再改：对齐到最初的正式模板
            t = reg.template(t.meta["base"])
        pid = project_id or self.studio.current_project()["id"]
        problems = check_graph(graph, self.orch.executor_for(t).object_info())

        slots, dropped = {}, []
        for name, s in t.slots.items():
            node = graph.get(s.node)
            if (
                node
                and node["class_type"] == t.graph[s.node]["class_type"]
                and s.field in node["inputs"]
            ):
                slots[name] = s.model_dump(exclude_defaults=True)
            else:
                dropped.append(name)
        tiers = {}
        for tn, tier in t.tiers.items():
            vals = {k: v for k, v in tier.values().items() if k in slots}
            tiers[tn] = {**vals, "verified": False}
        out_prefix = None
        if (
            t.output_prefix
            and t.output_prefix.node in graph
            and t.output_prefix.field in graph[t.output_prefix.node]["inputs"]
        ):
            out_prefix = t.output_prefix.model_dump()

        vid = uuid.uuid4().hex[:6]
        name = f"{t.name}~{vid}"
        d = self.dir / vid
        d.mkdir(parents=True)
        (d / "api.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2))
        tpl = {
            "name": name,
            "title": f"{t.title} · variant {vid}",
            "kind": t.kind,
            "model": t.model,
            "source": f"variant of {t.name}",
            "release_after": t.release_after,
            "slots": slots,
            "tiers": tiers,
            "default_tier": t.default_tier,
        }
        if out_prefix:
            tpl["output_prefix"] = out_prefix
        (d / "template.yaml").write_text(yaml.safe_dump(tpl, allow_unicode=True, sort_keys=False))
        meta = {
            "id": vid,
            "name": name,
            "base": t.name,
            "project_id": pid,
            "task_id": task_id,
            "note": note,
            "created": time.time(),
            "status": "invalid" if problems else "candidate",
            "problems": problems,
            "dropped_slots": dropped,
            "diff": diff_graph(self._diff_base(t, task_id), graph),
            "trial": None,
        }
        self._save(meta)
        reg.reload()
        self.studio.log(
            "web",
            "in",
            f"Imported workflow → {name}" + (f" ({len(problems)} problem(s))" if problems else ""),
            pid,
        )
        return meta

    def _diff_base(self, t, task_id: str | None) -> dict:
        """对比基准：导出的那个任务实际提交的 workflow（只显示你在 ComfyUI 里的改动）；没有则用模板。"""
        if task_id:
            p = self.store.task_dir(task_id) / "prompt.json"
            if p.exists():
                return json.loads(p.read_text())
        return t.graph

    # ---- 试跑 / 晋升 / 否决 ----
    def trial(self, vid: str, origin: str = "web") -> dict:
        meta = self.refresh(vid)
        if meta["status"] not in ("candidate", "trial_ok"):
            raise StudioError(f"variant is {meta['status']}; cannot run a trial")
        t = self.orch.registry.template(meta["name"])
        tier = min(t.tiers, key=lambda k: tier_cost(t.tiers[k].values()))
        rnd = self.studio.start_round(
            meta["name"],
            self.graph_params(t, meta),
            1,
            tier,
            f"Trial · {meta['name']}",
            origin=origin,
            request="workflow trial",
            project_id=meta["project_id"],
            allow_unverified=True,
        )
        meta["trial"] = {
            "round_id": rnd["id"],
            "tier": tier,
            "started": time.time(),
            "result": None,
        }
        return self._save(meta)

    def graph_params(self, t, meta: dict) -> dict:
        """试跑参数取自你在 ComfyUI 里改过的图本身（尺寸由档位决定）；
        参考图例外：图里是 ComfyUI 侧的文件名，回退到来源任务的本地路径。"""
        params = {}
        src = self.store.get(meta["task_id"]) if meta.get("task_id") else None
        for name, s in t.slots.items():
            if name in TIER_KEYS:
                continue
            if s.type == "image":
                if src and name in src["params"]:
                    params[name] = src["params"][name]
                continue
            params[name] = t.graph[s.node]["inputs"][s.field]
        return params

    def refresh(self, vid: str) -> dict:
        meta = self.get(vid)
        tr = meta.get("trial")
        if not tr or tr.get("result") or meta["status"] not in ("candidate", "trial_ok"):
            return meta
        tasks = self.store.round_tasks(tr["round_id"])
        if not tasks or any(t["state"] in RUNNING_STATES for t in tasks):
            return meta
        t = tasks[0]
        m = t["metrics"] or {}
        ok = t["state"] in ("review", "done", "rejected") and bool(t["outputs"])
        tr["result"] = {
            "ok": ok,
            "task_id": t["id"],
            "error": t["error"],
            "exec_s": m.get("exec_s"),
            "min_avail_gib": (m.get("memory") or {}).get("min_avail_gib"),
            "peak_gib": round((m.get("freevideo") or {}).get("ram_peak_bytes", 0) / 2**30, 1)
            or None,
        }
        meta["status"] = "trial_ok" if ok else "candidate"
        self._save(meta)
        return meta

    def promote(self, vid: str, name: str, title: str | None = None) -> dict:
        meta = self.refresh(vid)
        if meta["status"] != "trial_ok":
            raise StudioError("run a successful trial before promoting")
        if not NAME_RE.match(name):
            raise StudioError("name: lowercase letters, digits and dashes (2–41 chars)")
        if name in self.orch.registry.templates:
            raise StudioError(f"template {name} already exists")
        src, dst = self.dir / vid, self.user_dir / name
        dst.mkdir(parents=True)
        shutil.copy2(src / "api.json", dst / "api.json")
        tpl = yaml.safe_load((src / "template.yaml").read_text())
        tpl["name"] = name
        tpl["title"] = title or tpl["title"].replace(f" · variant {vid}", f" · {name}")
        tpl["tiers"][meta["trial"]["tier"]]["verified"] = True  # 只有试跑过的档位算已实测
        tpl["source"] = f"promoted from variant {vid} of {meta['base']}"
        (dst / "template.yaml").write_text(yaml.safe_dump(tpl, allow_unicode=True, sort_keys=False))
        meta.update(status="promoted", promoted_as=name)
        self._save(meta)
        self.orch.registry.reload()
        self.studio.log(
            "web", "in", f"Promoted workflow {meta['name']} → template {name}", meta["project_id"]
        )
        return meta

    def reject(self, vid: str) -> dict:
        meta = self.get(vid)
        for t in self.store.by_state("draft"):
            if t["template"] == meta["name"]:
                self.store.transition(t["id"], "failed", "variant rejected", error="cancelled")
        meta["status"] = "rejected"
        self._save(meta)
        self.orch.registry.reload()
        return meta
