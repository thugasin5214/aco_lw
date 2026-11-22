---
name: storyboard-film
description: Break an approved adaptation into a shot list for a FreeVideo film — who is in each shot, framing, camera, one clear action, spoken lines with speakers, duration and whether the shot continues the previous one. Use after the cast cards exist.
---

# Storyboard for a FreeVideo film

`inputs.adaptation` has the scenes; `inputs.bible` the characters (ids, looks, voices) and style;
`inputs.cards.characters` maps character ids to their FreeVideo cast cards — only those characters
can be cast with a consistent face. Each shot becomes one H3 video take (with the cast cards as
references); the user picks the best take per shot.

## Follow the direction (`inputs.direction`)
The director's book is binding: use its `camera_grammar` (prefer / avoid, motion budget, height),
`shot_size_bias`, `lens_policy`, `composition`, `light_and_color` and `editing.avg_shot_seconds`
for every shot, and keep its `continuity` rules (axis, screen direction, eyelines). For each scene
follow the matching `direction.scenes[]` entry (same id): its `camera_strategy`, `shot_families`,
the strongest camera choice on its `key_moment`, and its `transition_out`. Carry the `motifs`.
When the book and the "Craft" defaults below disagree, the book wins.

## Per shot
- `id`: `sh01`, `sh02`, … in screen order. `scene`: the scene id.
- `cast`: ids of the characters visible (at most 3). Only ids present in `inputs.cards.characters`.
- `frame`: what the camera sees at the start — subjects, setting, light, wardrobe — in concrete
  visual English. Refer to people by name ("Miss Malfoy", "Harry"); their faces come from the cards,
  so do not re-describe facial features, but do keep wardrobe identical across shots.
- `framing`: shot size and angle ("medium two-shot, eye level").
- `camera`: one move in H3 terms: static shot, push in, pull out, pan, truck, tilt, tracking shot,
  arc shot, with amplitude and speed ("slow push in with small amplitude").
- `motion`: the single action that happens during the shot. One clear action per shot.
- `seconds`: 4–15. A shot that continues the previous one must be **at least 10 s**.
- `continue`: true when the shot is the same moment and place as the previous shot, carried on
  from its last frame (real video continuation — best for keeping faces, light and positions).
  Start a new scene, a time jump or a big change of angle without `continue`.
- `lines`: spoken lines, verbatim, in `settings.language`: `{who: character id, line, how}`
  ("how" = delivery, e.g. "dry, amused"). Lines must fit the duration (about 2.5 words/second).

## One scene at a time (`inputs.item`)
When `inputs.item` is present you storyboard **only that scene** (`inputs.chunk.index` of
`inputs.chunk.of`); the orchestrator joins the scenes and renumbers the ids, so any ids will do.
- Cover **every** beat and **every** line of dialogue of the scene, in order — nothing skipped.
- Fill about `inputs.chunk.target_seconds` seconds of screen time with your shots (sum of
  `seconds`). Use more shots rather than longer ones when a scene has many beats.
- `inputs.previous_element` is the last shot of the previous scene: keep wardrobe and light
  consistent, and do not set `continue` on your first shot (a new scene starts fresh).

## Long episodes (`settings.episode_minutes`)
- A 5-minute episode is roughly 35–50 shots. Detail comes from more shots — inserts of hands and
  objects, reactions, over-the-shoulder exchanges — not from cramming actions into one shot.
- Interior monologue (`settings.interiority: visual`): show it — a close-up on the eyes, a
  hesitation, a glance at an object, a slow push in during a silence. No voice-over lines.

## Craft
- 6–12 shots for about a minute (more for long episodes, above); vary sizes; respect the 180° rule.
- Concrete sights and sounds over adjectives ("strings of warm bulbs flicker on" not "magical").
- Keep it wholesome: affectionate, never intimate beyond holding hands, a hug or a head on a shoulder.
- Follow the reference skill's guidance on camera language, dialogue and timing where it applies.

## Revisions
Apply `revision_request`; keep ids and text of untouched shots identical.
