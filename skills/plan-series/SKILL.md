---
name: plan-series
description: Plan a novel as a series of episodes from per-chapter notes — which chapters make each episode, titles, synopses, length and cliffhangers. Use after the chapter notes of the novel-to-series workflow.
---

# Plan the series

`inputs.notes.chapters` are notes on every chapter in order (summary, events, characters,
`screen_minutes`). Split the novel into **episodes** of about `settings.episode_minutes` minutes of
screen time each.

- Every chapter goes into exactly one episode, in order; never skip or reorder chapters.
- Sum the chapters' `screen_minutes` per episode and aim for `settings.episode_minutes`
  (±40 %). A chapter longer than that is its own episode (it will simply run longer); short
  chapters are combined.
- Prefer episode breaks at natural turns — a revelation, a decision, a change of place or time —
  so each episode ends on a hook (`cliffhanger`).
- `id`: `e01`, `e02`, … `title`: short. `synopsis`: 3–6 sentences in the novel's language; it is
  passed to later episodes as "previously on", so name characters exactly as the notes do.
- `minutes`: the estimated screen time.
- Top level: `title` of the series, a one-sentence `logline`, and a short `synopsis` of the whole.
