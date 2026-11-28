"""Score a finished bench run: export → aco metrics → faces → online judge (→ VAHBench)."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT
from ..store import Store
from . import export as exporter
from . import frames, metrics


def _lora_bases(cfg) -> dict[str, str]:
    try:
        from ..assets import LoraCatalog
        from ..registry import Registry

        return {
            r["file"]: r["base"]
            for r in LoraCatalog(cfg, Registry.from_settings(cfg)).items()
            if r.get("base")
        }
    except Exception:  # noqa: BLE001 — the LoRA check then only reports what was used
        return {}


def _reference(case: dict, store: Store, project_id: str) -> Path | None:
    for c in case.get("characters") or []:
        if c.get("reference"):
            p = Path(c["reference"]).expanduser()
            return p if p.is_absolute() else ROOT / p
    images = []
    for r in store.rounds(project_id):
        for t in store.round_tasks(r["id"]):
            for o in t["outputs"] or []:
                if o["kind"] == "image":
                    images.append((t["created"], store.task_dir(t["id"]) / o["file"]))
    return min(images)[1] if images else None


def score(
    run_dir: Path,
    cfg,
    use_judge: bool = True,
    use_faces: bool = True,
    use_vahbench: bool = False,
    judge_model: str | None = None,
) -> dict:
    store = Store(cfg.db_path, cfg.jobs_dir)
    info = json.loads((run_dir / "run.json").read_text())
    case = info["case"]
    export_root = cfg.data_dir / "bench" / "export"
    dest = exporter.export(run_dir, store, export_root)
    m = metrics.collect(run_dir, store, _lora_bases(cfg))
    episodes = m["delivered"]

    sheets, ep_frames = [], {}
    for e in episodes:
        video = dest / e["file"]
        fr = frames.sample(video, run_dir / "frames", 12)
        ep_frames[e["episode"]] = fr
        sheet = frames.contact_sheet(fr, run_dir / "sheets" / f"episode{e['episode']}.jpg")
        if sheet:
            sheets.append(sheet)

    if use_faces and episodes:
        try:
            from . import faces

            ref = _reference(case, store, info["project_id"])
            m["faces"] = {
                "reference": str(ref) if ref else None,
                "episodes": {str(k): faces.score(ref, v) for k, v in ep_frames.items()},
            }
        except ImportError as e:
            m["faces"] = {"error": f"insightface not installed (uv sync --extra bench): {e}"}

    if use_judge:
        from .judge import judge

        steers = [
            json.loads(x)["text"]
            for x in (run_dir / "events.jsonl").read_text().splitlines()
            if x.strip()
            and json.loads(x)["kind"] == "user"
            and json.loads(x)["why"].startswith("steer")
        ]
        m["judge"] = judge(
            case,
            episodes,
            sheets,
            steers,
            m["final_reply"],
            run_dir / "judge",
            cfg.claude_cmd,
            judge_model,
        )

    if use_vahbench:
        from . import vahbench

        m["vahbench"] = vahbench.score(
            cfg.data_dir, export_root, run_dir / "vahbench", run_filter=dest.name
        )

    m["export_dir"] = str(dest)
    (run_dir / "score.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    return m
