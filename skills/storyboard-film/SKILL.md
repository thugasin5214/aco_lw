---
name: storyboard-film
description: Break an approved adaptation into a shot list for a FreeVideo film — who is in each shot, framing, camera, one clear action, spoken lines with speakers, duration and whether the shot continues the previous one. Use after the cast cards exist.
---

# Storyboard for a FreeVideo film

`inputs.adaptation` has the scenes; `inputs.bible` the characters (ids, looks, voices) and style;
`inputs.cards.characters` maps character ids to their FreeVideo cast cards — only those characters
can be cast with a consistent face. Each shot becomes one H3 video take (with the cast cards as
references); the user picks the best take per shot.

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

## Craft
- 6–12 shots for about a minute; vary sizes; respect the 180° rule.
- Concrete sights and sounds over adjectives ("strings of warm bulbs flicker on" not "magical").
- Keep it wholesome: affectionate, never intimate beyond holding hands, a hug or a head on a shoulder.
- Follow the reference skill's guidance on camera language, dialogue and timing where it applies.

## Revisions
Apply `revision_request`; keep ids and text of untouched shots identical.
