"""VAHBench (github.com/TokenRhythm/VAHBench, MIT) on aco's runs.

`setup()` clones it into data/bench/VAHBench with its own virtualenv (it is not a dependency of
aco). `score()` runs its evaluator on an exported folder (export.py). Its trajectory extractor
calls an LLM API only for the checklist fields its rules cannot read; here that call goes through
Claude Code (`claude -p`, your login) via a small shim, so no API key is needed. The paper's
detectors are not public: VAHBench uses its reference detectors, so results are labelled partial
and are not comparable with the paper's numbers (its own README says so).

`import_briefs()` turns VAHBench's five briefs (from its Hugging Face dataset) into bench cases.
"""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

REPO = "https://github.com/TokenRhythm/VAHBench.git"
DATASET = "TokenRhythm/VAHBench"

SHIM = r"""
import json, subprocess, sys
sys.path.insert(0, ".")
import stage1_inputs.client as client

def make_call_fn(model="claude", *, provider=None, timeout=300.0, log=None, max_tokens=32768):
    if log is not None:
        log.provider, log.model_requested = "claude_code", "claude-code"
    def call_fn(prompt):
        p = subprocess.run(["claude", "-p", "--output-format", "json", "--tools", "",
                            "--no-session-persistence"], input=prompt, text=True,
                           capture_output=True, timeout=timeout, check=False)
        obj = json.loads(p.stdout or "{}")
        if log is not None:
            log.n_calls += 1
            log.total_prompt_chars += len(prompt)
        if obj.get("is_error"):
            raise RuntimeError(str(obj.get("result"))[:300])
        return obj.get("result") or ""
    return call_fn

client.make_call_fn = make_call_fn
client.available = lambda: ["claude_code"]
client.resolve_provider = lambda preferred=None: ("claude_code", "", "")
import run
sys.argv = ["run.py", *sys.argv[1:]]
sys.exit(run.main())
"""


def home(data_dir: Path) -> Path:
    return data_dir / "bench" / "VAHBench"


def python(data_dir: Path) -> Path:
    return home(data_dir) / ".venv" / "bin" / "python"


def setup(data_dir: Path) -> Path:
    h = home(data_dir)
    if not h.exists():
        h.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", REPO, str(h)], check=True)
    if not python(data_dir).exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(h / ".venv")], check=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python(data_dir)), "-e", f"{h}[reference]"],
            check=True,
        )
    (h / "aco_shim.py").write_text(SHIM)
    return h


def score(data_dir: Path, export_root: Path, out_dir: Path, run_filter: str | None = None) -> dict:
    h = setup(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(python(data_dir)),
        "aco_shim.py",
        "--data",
        str(export_root),
        "--out-dir",
        str(out_dir),
        "--allow-partial",
    ]
    if run_filter:
        args += ["--run", run_filter]
    proc = subprocess.run(args, cwd=h, capture_output=True, text=True, check=False)
    (out_dir / "vahbench.log").write_text(
        proc.stdout[-20000:] + "\n--- stderr ---\n" + proc.stderr[-20000:]
    )
    rows = []
    summary = out_dir / "summary.csv"
    if summary.exists():
        with summary.open() as f:
            rows = list(csv.DictReader(f))
    return {"exit": proc.returncode, "rows": rows, "out": str(out_dir)}


def import_briefs(cases_dir: Path) -> list[Path]:
    """VAHBench's five briefs (its Hugging Face dataset, benchmark/prompts.jsonl) → bench cases in
    `cases_dir` (data/bench/cases: downloaded data, kept out of the repo)."""
    import httpx
    import yaml

    url = f"https://huggingface.co/datasets/{DATASET}/resolve/main/benchmark/prompts.jsonl"
    r = httpx.get(url, follow_redirects=True, timeout=60)
    r.raise_for_status()
    cases_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for line in r.text.splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        slug = Path(b["requirements_file"]).stem.split("_", 1)[1]
        case = {
            "id": f"vahbench-{b['brief_id'].lower()}",
            "title": f"VAHBench {b['brief_id']} · {b['title']}",
            "source": "vahbench",
            "prompt_dir": f"prompt_{b['prompt_no']:02d}_{slug}",
            "brief": b["prompt"],
            "target": {
                "episodes": b["episodes"],
                "seconds_per_episode": b["seconds_per_episode"],
                "aspect_ratio": b["aspect_ratio"],
            },
            # VAHBench gives no human: the agent runs on its own (no steering)
            "user": {"approvals": "approve", "answer": "Decide yourself and continue."},
            "limits": {"hours": 4 + 2 * b["episodes"] * b["seconds_per_episode"] / 60},
        }
        f = cases_dir / f"{case['id']}.yaml"
        f.write_text(yaml.safe_dump(case, allow_unicode=True, sort_keys=False))
        written.append(f)
    return written
