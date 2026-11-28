"""Bench cases: a brief, what must be delivered, what a scripted user says and when, what to check.

```yaml
id: s01-red-umbrella
title: The red umbrella
brief: |                      # what the user sends first (in the user's words)
  ...
target: {episodes: 1, seconds_per_episode: 30, aspect_ratio: "9:16"}
characters:                   # for face consistency; reference optional (else the first casting image)
  - {name: Lin, reference: null}
expect:
  must_keep: ["Lin finds the umbrella", "the ending happens at the bus stop"]   # judge checklist
  loras: []                   # LoRA files the case expects to be used (substring match)
  workflows: []               # saved ComfyUI workflows that fit (substring match), for reporting
user:
  approvals: approve          # approve | decline (installs / downloads are always declined)
  answer: "Your call — go ahead."   # reply to ask_user
  pick: 1                     # which candidate to pick when a round waits for the user
  steer:                      # messages sent mid-run
    - {after: first_video, say: "Make the last shot happen at night in the rain.",
       check: "the last shot is at night, in the rain"}
limits: {hours: 6}
```

`after`: first_image | first_video | first_round | run_done | seconds:N | stage:<id>.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..config import ROOT

CASES_DIR = ROOT / "bench" / "cases"


class Target(BaseModel):
    episodes: int = 1
    seconds_per_episode: float = 30
    aspect_ratio: str = "16:9"


class Character(BaseModel):
    name: str
    reference: str | None = None


class Expect(BaseModel):
    must_keep: list[str] = []
    loras: list[str] = []
    workflows: list[str] = []


class Steer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after: str
    say: str
    check: str = ""


class User(BaseModel):
    approvals: str = "approve"
    answer: str = "Your call — go ahead."
    pick: int = 1
    steer: list[Steer] = []


class Limits(BaseModel):
    hours: float = 6


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    source: str = "aco"  # aco | vahbench
    brief: str
    target: Target = Field(default_factory=Target)
    characters: list[Character] = []
    expect: Expect = Field(default_factory=Expect)
    user: User = Field(default_factory=User)
    limits: Limits = Field(default_factory=Limits)
    # VAHBench's prompt folder name (prompt_01_...) for export; ours get prompt_<id>
    prompt_dir: str | None = None

    @property
    def folder(self) -> str:
        return self.prompt_dir or f"prompt_{self.id.replace('-', '_')}"

    def delivery_note(self) -> str:
        t = self.target
        return (
            f"\n\nDeliver {t.episodes} finished episode file(s), about {t.seconds_per_episode:g} s "
            f"each, aspect {t.aspect_ratio}. When they are done, send them to me with send_file "
            "(one file per episode, in order) and tell me what you made."
        )


def _files(extra: Path | None) -> list[Path]:
    dirs = [CASES_DIR, *([extra] if extra else [])]
    return [f for d in dirs if d.is_dir() for f in sorted(d.glob("*.yaml"))]


def load(case_id: str, extra: Path | None = None) -> Case:
    for f in _files(extra):
        data: Any = yaml.safe_load(f.read_text())
        if data and data.get("id") == case_id:
            return Case(**data)
    raise KeyError(f"no bench case {case_id}")


def all_cases(extra: Path | None = None) -> list[Case]:
    """Our cases (bench/cases) and imported ones (data/bench/cases)."""
    return [Case(**yaml.safe_load(f.read_text())) for f in _files(extra)]
