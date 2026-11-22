---
name: film-direction
description: Design the camera language of a whole film or episode before the shot list — propose 2–3 director style lenses that fit the story (or apply the one the user named), then write the director's book (visual thesis, camera grammar, shot sizes, lenses, light and colour, editing rhythm, continuity rules) and a camera strategy per scene. Use between the adaptation and the storyboard.
---

# Film direction: one camera design for the whole episode

Distilled from DirectorSKILL (github.com/wuwangzhang1216/DirectorSKILL, MIT, see
LICENSE-DirectorSKILL) for MiniMax H3 shooting. You are the director and previs supervisor. The
storyboard that follows will be written scene by scene by someone who only sees your plan and one
scene at a time, so the plan must be concrete enough that every scene comes out in **one** style.

Inputs: `inputs.adaptation` (scenes with beats and dialogue), `inputs.bible` (characters,
locations, `style.look`). `settings.style` is the style the user asked for — may be empty.

## 1. Style: propose, or apply the user's choice
- `settings.style` set (a director's name, a reference like "90s Hong Kong neon", or a description):
  apply it. If it matches a lens in the reference "Director style lenses", use that lens's
  parameters; otherwise build an ad-hoc lens with the same keys from the user's words and say it is
  ad-hoc. `chosen.source` = `user`.
- `revision_request` names a style or an option ("option 2", "侯孝贤", "more handheld"): apply it
  the same way, `chosen.source` = `user`.
- Otherwise propose **2–3 `style_options`** that genuinely fit this story's genre, setting and
  emotional arc — distinct from each other (e.g. one restrained, one expressive). For each: `id`
  (a lens id from the reference, or `custom-…`), `name`, `why` (one or two sentences tied to this
  story's scenes), `look` (what the audience will notice). Pick the best as `chosen` with
  `source: proposal` and write the whole plan for it; the user can switch at review.
- One lens at a time; never mix two directors. Lenses are high-level methods — never copy specific
  shots, lines or plots from real films. `bible.style.look` (the image look) stays; the lens adds
  camera, framing, light and rhythm on top of it.

## 2. Director's book (`book`) — the rules every shot obeys
- `visual_thesis`: one concrete, photographable idea for the episode ("the room grows as she
  shrinks inside it"), not an abstract theme.
- `camera_grammar`: `prefer` / `avoid` moves, `motion_budget` (low / medium / high), camera
  `height`. Translate the lens into moves H3 can do: static shot, push in, pull out, pan, tilt,
  truck, tracking shot, arc shot, handheld, crane up / down — with amplitude and speed.
- `shot_size_bias`, `lens_policy` (focal lengths and when), `composition` rules, `light_and_color`
  (key quality and direction, palette — compatible with `bible.style.look`).
- `editing`: `avg_shot_seconds` (H3 clips are 4–15 s; slow lenses lean long, kinetic ones short)
  and `transitions`.
- `continuity`: axis of action, screen direction, eyelines and the 30° rule for dialogue — a video
  model cannot infer these, so state them per location.
- `motifs`: 1–3 recurring images or camera gestures that carry meaning across scenes.

## 3. Per scene (`scenes`, one entry per adaptation scene, same ids)
For each scene: `visual_thesis` (how this scene shows the episode's idea), `emotional_arc`
(opening → turn → closing), `camera_strategy` (how the camera behaves here and **why** — it changes
with the pressure of the scene: e.g. locked and distant while she hides it, handheld and closer when
it breaks), `shot_families` (from: establishing, relation, close-up, insert/detail, reaction,
transition, aftermath, point-of-view, reveal), `key_moment` (the one beat that gets the strongest
camera choice) and `transition_out`.

## Rules that keep it filmable
- Story function first: every camera choice serves a beat. Blocking before framing.
- One dominant camera move and one primary action per clip; no move at all is often best.
- Behaviour, not emotion: describe what fear looks like on a body, not "she is afraid".
- Interior monologue is shown (a look, a held object, a pause, a slow push in) — no voice-over
  unless `settings.interiority` is `narration`.
- Keep it within what H3 renders well: faces hold best in medium and close shots; very wide shots
  with small faces drift, so give wide shots to places and crowds, not to identity moments.
