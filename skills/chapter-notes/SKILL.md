---
name: chapter-notes
description: Read one chapter (or part) of a novel and write compact notes — summary, events in order, every character with the appearance the text gives, locations, mood and estimated screen time. Use as the reading pass of the novel-to-series workflow, one chapter per call.
---

# Chapter notes

`inputs.item` is one chapter of a longer novel: `id`, `title`, `text`. You see only this chapter;
`inputs.previous_element` (if present) is your note on the chapter before, for continuity of names.

Reply with `{"chapters": [ one entry ]}` for this chapter only, `id` copied from `inputs.item.id`.

- `summary`: 2–5 sentences, what happens and how it turns.
- `events`: the scenes / events in order, one line each, concrete (who does what, where).
- `characters`: everyone who appears or speaks. `name` as the text names them (keep one spelling
  across chapters — reuse the spelling from `previous_element`), `aliases` (nicknames, titles,
  pronoun-only references you resolved), `role` in this chapter, and `appearance`: the physical
  description and clothing **as the text states it** (quote or close paraphrase; empty if the
  chapter gives none — do not invent looks here).
- `locations`: named or clearly distinct places.
- `mood`: one line.
- `screen_minutes`: how long this chapter would run filmed faithfully (every scene and line kept);
  roughly 1 minute per 600–900 Chinese characters or 400–600 English words of scene text,
  less for narration-heavy passages.

Write the notes in the language of the chapter.
