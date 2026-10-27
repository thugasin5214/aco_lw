---
name: story-bible
description: Build the visual bible for a film — a global style, plus a look description for every recurring character and location — so every later image and video stays consistent. Use after the adaptation is approved.
---

# Story bible: style, characters, locations

From `inputs.adaptation` (and `inputs.source` for detail), define everything that must stay
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
- Never describe a real, identifiable person.

## Locations (`locations`, 1–6)
- `id`, `name`, `look`: architecture / landscape, materials, era, weather, time of day.
  No people in the description.

## Revisions
Apply `revision_request` to the named entries only; keep ids and untouched entries byte-identical
so already-approved character sheets are not invalidated.
