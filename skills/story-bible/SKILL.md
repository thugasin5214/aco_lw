---
name: story-bible
description: Build the visual bible for a film — a global style, plus a look description for every recurring character and location — so every later image and video stays consistent. Use after the adaptation is approved.
---

# Story bible: style, characters, locations

From `inputs.adaptation` (and `inputs.brief.text` for detail), define everything that must stay
**identical across shots**. These descriptions are pasted verbatim into image prompts, so write
them as concrete visual descriptions, not prose.

## Style (`style`)
- `look`: one line that fixes medium, era, lighting and finish — e.g. "35mm film still, soft
  overcast light, muted teal and amber grade, shallow depth of field". Every prompt ends with it.
- `palette`, `lens`: optional extra constraints.

## Characters (`characters`, at most 6 — main cast only)
- `id`: lowercase slug (`mei`, `old_fisherman`); used as a reference key by later stages.
- `look`: age, build, face, hair, skin, distinctive marks **and** default wardrobe in one dense
  sentence. Avoid anything that changes between shots (expressions, poses, actions).
- Do not base a look on a real, identifiable person unless the user provided their photo as a
  reference (see below).

## Locations (`locations`, 1–6)
- `id`, `name`, `look`: architecture / landscape, materials, era, weather, time of day.
  No people in the description.

## Provided reference images (`inputs.brief.references`)
Each has an `id`, `label`, `role` (character / location / style / prop), `use` and optional `note`.
- When a character or location corresponds to a reference (by label, role and note), set its
  `reference` to that id. The pipeline then uses the user's picture instead of inventing a look —
  as is, or redrawn in the film's style. Still write `look`, consistent with the label and note.
- Put the ids of `style` references in `style.references`, and let them inform `style.look`.
- Never attach one reference to two entries, and never invent ids.
- These may be photos of real people the user chose to cast; describe them only visually, as with
  any character.

## Revisions
Apply `revision_request` to the named entries only; keep ids and untouched entries byte-identical
so already-approved character sheets are not invalidated.
