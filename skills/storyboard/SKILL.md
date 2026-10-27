---
name: storyboard
description: Break an approved adaptation into a shot list — framing, camera movement, motion, duration and which approved reference (character sheet or location plate) anchors each shot's keyframe. Use after characters and locations are cast.
---

# Storyboard: the shot list

Turn `inputs.adaptation` scenes into an ordered list of **shots**. Each shot becomes one keyframe
image (edited from a reference sheet) and then one image-to-video clip of a few seconds.

## Per shot
- `id`: `sh01`, `sh02`, … in screen order. `scene`: the scene id it belongs to.
- `ref`: the approved reference that anchors the keyframe — `characters/<id>` when a character is
  the subject, otherwise `locations/<id>`. Only use ids that exist in `inputs.bible` and appear in
  `inputs.characters` / `inputs.locations` (those are the approved sheets).
- `cast`: character ids visible in the shot.
- `frame`: what the still image shows — subject, action at that instant, setting, light.
  Restate the character's key look in a few words; the style line is appended automatically.
- `framing`: shot size and angle (e.g. "medium close-up, eye level, rule of thirds").
- `camera`: one movement — static, slow push-in, pan left, tilt up, handheld drift, dolly out…
- `motion`: what moves during the clip (subject action, wind, light change). One clear action:
  AI video handles one simple motion per clip far better than a sequence.
- `seconds`: intended screen time. At the default `settings.video_tier` (smoke) every clip is
  about 3 s; longer beats need several shots.
- `dialogue`: the line spoken in this shot, if any (becomes a subtitle).
- `continuity`: what must match the previous shot (screen direction, props, light).

## Language
Write `frame`, `motion`, `camera` and `dialogue` in `settings.language` when it is set (for
example Mandarin Chinese); otherwise in the language of the source. The video model voices the
dialogue in the language the prompt is written in, so this decides what the characters speak.

## Craft rules
- Open each scene with an establishing or orienting shot; vary shot sizes; respect the 180° rule.
- Aim for 6–12 shots for a 1-minute film; never exceed 24.
- Keep `frame` free of camera words and `camera` free of content words.

## Revisions
Apply `revision_request` — it may concern one shot or all of them (e.g. "every shot in
Mandarin"). Keep ids stable, and keep the text of shots the request does not touch identical so
their approved keyframes and clips stay valid.
