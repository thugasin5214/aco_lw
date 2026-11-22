"""Novel → series: deterministic chapter splitting and spawning one production per episode.

A whole novel does not fit one local-LLM call, so the series workflow splits it in code
(`chapters`), lets the LLM read it chapter by chapter, plan episodes, and fix the cast, style and
camera design once. `series_spawn` then creates one `novel-episode` production per episode that
reuses those decisions, and the episodes run one after another.
"""

from __future__ import annotations

import re

MAX_CHARS = 6000  # one chapter part per LLM call (local context)

# 第十二章 / 第3回 / 卷二 / Chapter 7 / CHAPTER XII / Part Two — alone on a line
HEADING = re.compile(
    r"^[ \t#*]*("
    r"第[一二三四五六七八九十百千零〇两\d]+[章回节卷集部]"
    r"|卷[一二三四五六七八九十百千\d]+"
    r"|(?:chapter|part)\s+[\w-]+"
    r")[^\n]{0,40}$",
    re.IGNORECASE | re.MULTILINE,
)

# episode_review → stages of each episode that pass review automatically
REVIEW_LEVELS = {
    "final": ["adaptation", "storyboard", "clips", "edit"],  # you only review the final cut
    "storyboard": ["adaptation", "clips", "edit"],  # you also review each storyboard
    "all": [],
}
# series-only settings that are not passed on to the episodes
SERIES_ONLY = {"episode_review", "auto_shoot", "chapter_chars"}


def _parts(title: str, text: str, limit: int) -> list[tuple[str, str]]:
    """Split a long chapter at paragraph boundaries into parts of at most ~limit characters."""
    if len(text) <= limit:
        return [(title, text)]
    paras, parts, cur = re.split(r"\n\s*\n", text), [], ""
    for p in paras:
        if cur and len(cur) + len(p) > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        parts.append(cur)
    out = []
    for part in parts:
        # a single paragraph far longer than the limit is cut hard
        out += (
            [part[j : j + limit] for j in range(0, len(part), limit)]
            if len(part) > 2 * limit
            else [part]
        )
    n = len(out)
    return [(f"{title} ({i}/{n})" if n > 1 else title, t) for i, t in enumerate(out, 1)]


def split_chapters(text: str, limit: int = MAX_CHARS) -> list[dict]:
    """Chapters by their headings (Chinese and English styles); without headings, by length.
    Long chapters are split into parts. Returns [{id, title, chars, text}]."""
    text = (text or "").strip()
    if not text:
        return []
    marks = list(HEADING.finditer(text))
    pieces: list[tuple[str, str]] = []
    if len(marks) >= 2:
        head = text[: marks[0].start()].strip()
        if len(head) > 200:  # a real prologue, not just a book title
            pieces.append(("Prologue", head))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end() : end].strip()
            if body:
                pieces.append((m.group(0).strip(" \t#*"), body))
    else:
        pieces.append(("Part", text))
    chapters = []
    for title, body in pieces:
        for t, part in _parts(title, body, limit):
            chapters.append({"title": t, "text": part})
    for n, c in enumerate(chapters, 1):
        c["id"] = f"c{n:03d}"
        c["chars"] = len(c["text"])
        if c["title"] == "Part":
            c["title"] = f"Part {n}"
    return chapters


class SeriesTools:
    """Tool stages of the series workflow, registered on the production engine."""

    def __init__(self, engine):
        self.engine = engine

    def chapters(self, prod: dict, st, inputs: dict) -> dict:
        brief = inputs.get("brief") or {}
        limit = int(prod["settings"].get("chapter_chars") or MAX_CHARS)
        return {"chapters": split_chapters(brief.get("text") or "", limit)}

    def episodes(self, prod: dict) -> list[dict]:
        """This series' episode productions, in episode order."""
        rows = [
            p
            for p in self.engine.store.productions(prod["project_id"])
            if (p["settings"].get("series") or {}).get("parent") == prod["id"]
        ]
        return sorted(rows, key=lambda p: p["settings"]["series"]["index"])

    def spawn(self, prod: dict, st, inputs: dict) -> dict:
        """One `novel-episode` production per planned episode, reusing the series' cast cards,
        bible and direction. Only the first episode starts; each next one starts when the previous
        one has finished shooting (see `handoff`). Rerunning keeps episodes that already exist."""
        plan = inputs["plan"]
        chapters = {c["id"]: c for c in (inputs.get("chapters") or {}).get("chapters", [])}
        settings = {k: v for k, v in prod["settings"].items() if k not in SERIES_ONLY}
        settings["auto_approve"] = REVIEW_LEVELS.get(
            prod["settings"].get("episode_review", "final"), REVIEW_LEVELS["final"]
        )
        settings["auto_start"] = ["clips"] if prod["settings"].get("auto_shoot") else []
        direction = inputs.get("direction") or {}
        per_episode = {s.get("id"): s for s in direction.get("scenes", [])}
        existing = {p["settings"]["series"]["index"]: p for p in self.episodes(prod)}
        eps = plan.get("episodes", [])
        made = []
        for i, ep in enumerate(eps):
            if i in existing:
                made.append(existing[i]["id"])
                continue
            text = "\n\n".join(
                f"{chapters[c]['title']}\n\n{chapters[c]['text']}"
                for c in ep.get("chapters", [])
                if c in chapters
            )
            series = {
                "parent": prod["id"],
                "index": i,
                "of": len(eps),
                "title": ep.get("title", ""),
                "synopsis": ep.get("synopsis", ""),
                "previously": [e.get("synopsis", "") for e in eps[:i]][-5:],
                "next": eps[i + 1].get("synopsis", "") if i + 1 < len(eps) else "",
            }
            child = self.engine.create(
                "novel-episode",
                f"{prod['title']} · E{i + 1:02d} {ep.get('title', '')}".strip(),
                {
                    "source": {"text": text, "sources": []},
                    "series": series,
                    "bible": inputs.get("bible") or {},
                    "cards": inputs.get("cards") or {},
                    "direction": {
                        **{k: v for k, v in direction.items() if k != "scenes"},
                        "scenes": [],
                        "episode_strategy": per_episode.get(ep.get("id"), {}),
                    },
                },
                {**settings, "series": {"parent": prod["id"], "index": i}},
                project_id=prod["project_id"],
            )
            if i > 0:  # waits for the previous episode (handoff)
                self.engine.pause(child["id"])
            made.append(child["id"])
        return {"episodes": made}

    def handoff(self, prod: dict, plan: dict) -> None:
        """An episode finished shooting (its clips are complete) → start the next paused one."""
        info = prod["settings"].get("series") or {}
        if not info or prod["settings"].get("series_handed_off"):
            return
        clips = next((s for s in plan["stages"] if s["id"] == "clips"), None)
        if not (plan["done"] or (clips and clips["state"] == "completed")):
            return
        self.engine.store.update_production(
            prod["id"], settings={**prod["settings"], "series_handed_off": True}
        )
        parent = self.engine.store.get_production(info["parent"])
        if parent is None:
            return
        nxt = next(
            (
                p
                for p in self.episodes(parent)
                if p["settings"]["series"]["index"] == info["index"] + 1
            ),
            None,
        )
        if nxt is not None and nxt["state"] == "paused":
            self.engine.log(f"series {parent['id']}: episode {info['index'] + 2} starts")
            self.engine.resume(nxt["id"])
