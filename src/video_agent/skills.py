"""Skill library: compatible with Claude Code's SKILL.md format (YAML frontmatter + body + references/).

Sources (later ones override same-named skills from earlier ones):
- built-in: repo `skills/`
- user: `data/skills/` (copy external skill directories in; ~/.claude/skills is not scanned by default)

Skill stages of production workflows reference skills by name. A skill only produces data (validated against the stage's JSON Schema)
and has no side effects; scripts inside a skill directory are never run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: Path
    origin: str
    meta: dict = field(default_factory=dict)

    def references(self) -> dict[str, str]:
        """Markdown under references/, appended to the prompt (the skill's own background material)."""
        ref = self.dir / "references"
        if not ref.is_dir():
            return {}
        return {p.name: p.read_text() for p in sorted(ref.glob("*.md"))}

    def prompt(self) -> str:
        parts = [self.body.strip()]
        for name, text in self.references().items():
            parts.append(f"## Reference: {name}\n\n{text.strip()}")
        return "\n\n".join(parts)


def parse_skill(path: Path, origin: str) -> Skill:
    text = path.read_text()
    meta: dict = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    name = meta.get("name") or path.parent.name
    return Skill(
        name=name,
        description=str(meta.get("description", "")).strip(),
        body=body,
        dir=path.parent,
        origin=origin,
        meta=meta,
    )


class SkillLibrary:
    def __init__(self, *roots: tuple[Path | None, str]):
        self.roots = roots
        self.reload()

    @classmethod
    def from_settings(cls, cfg) -> SkillLibrary:
        return cls((cfg.skills_dir, "builtin"), (cfg.data_dir / "skills", "user"))

    def reload(self) -> None:
        skills: dict[str, Skill] = {}
        for root, origin in self.roots:
            if not root or not Path(root).is_dir():
                continue
            for p in sorted(Path(root).glob("*/SKILL.md")):
                s = parse_skill(p, origin)
                skills[s.name] = s
        self.skills = skills

    def get(self, name: str) -> Skill:
        if name not in self.skills:
            raise KeyError(f"未知 skill {name}；可选：{', '.join(self.skills) or '（无）'}")
        return self.skills[name]

    def list(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description, "origin": s.origin}
            for s in self.skills.values()
        ]
