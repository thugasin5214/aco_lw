---
name: edit-timeline
description: Cut the approved shot clips into a timeline — order, trims, transitions and subtitles — producing an edit decision list that FFmpeg renders. Use when all shot videos are approved.
---

# Edit: build the timeline

You are the editor. `inputs.storyboard.shots` is the intended order; `inputs.clips` maps each shot
id to its approved video (with its parameters). Produce an edit decision list; you never write
FFmpeg commands — the pipeline renders your list deterministically.

## Output
- `clips`: ordered list of `{shot, in, out, subtitle}`.
  - `shot`: a shot id that exists in `inputs.clips`. You may drop weak shots or reorder for rhythm.
  - `in` / `out` (seconds, optional): trim dead frames at the head or tail; omit to use the
    whole clip. Clips are about 3 s at the smoke tier — trim at most ~0.5 s per side.
  - `subtitle`: the shot's dialogue (from the storyboard), if any. Keep it short.
- `transition`: `{type: "cut"}` by default; use `fade` / `dissolve` (0.2–0.6 s) only for
  time or place jumps.
- `notes`: one or two lines on the cut for the reviewer.

## Revisions
Apply `revision_request` precisely (e.g. "tighter", "drop sh04", "end on the close-up").
