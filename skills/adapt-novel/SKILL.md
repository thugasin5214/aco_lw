---
name: adapt-novel
description: Adapt prose fiction — or a short idea, web article or video transcript — into a short-film treatment with logline, synopsis and a compact list of filmable scenes with beats and key dialogue. Use as the first planning step of the novel-to-film workflow.
---

# Adapt prose into a short-film treatment

You are a screenwriter adapting the material in `inputs.brief` into a **short film**.
`inputs.brief.text` holds everything the user gave: a story, a one-line idea, web-page text, or a
YouTube title / description / transcript, each under its own heading. `inputs.brief.references`
lists reference images (you only see their labels and roles) — characters and places the user
already has pictures of; keep those names.

- A **story**: adapt it. A **short idea or description**: expand it into a story with a clear
  spine — invent only what the idea needs. **Articles / transcripts**: extract the story, facts or
  mood the user is after; do not copy long passages.
The output feeds a pipeline that renders each shot as a few seconds of AI video, so favour
visual storytelling over interior monologue.

## A series episode (`inputs.series`, `inputs.bible`)
The source is one episode's chapters of a longer novel. `inputs.series` has this episode's
`title` / `synopsis`, `previously` (synopses of earlier episodes) and `next`. Adapt **only this
episode's text**; use `previously` just to understand references, never re-stage earlier events.
Name characters and locations exactly as `inputs.bible` does (its `name`s; scene `characters` use
the bible ids) so the series' cast cards match. A character missing from the bible keeps the
novel's name.

## Fidelity (`settings.fidelity`)
- `condensed` (default): a short film — follow "How to adapt" below as written.
- `faithful`: the user wants **every detail** of the source on screen, as an episode of about
  `settings.episode_minutes` minutes. Then:
  - keep **every** scene, event and exchange of the source in order; do not merge or cut scenes,
    do not summarise several events into one beat. 5 minutes is roughly 12–25 scenes or
    sub-scenes; split a long scene where the place, time or focus changes.
  - beats are fine-grained: one observable action, reaction or line per beat, in source order —
    a detailed scene easily has 6–15 beats.
  - keep **all** spoken dialogue, verbatim (translated only when `settings.language` differs);
    long speeches may be split into several lines, not shortened.
  - the synopsis may be longer (up to ~300 words); the logline stays one sentence.
  - interior monologue and narration follow `settings.interiority`: `visual` (default) turns them
    into what a camera can see — a look, a gesture, a held object, a pause, a reaction shot —
    never a voice-over; `narration` keeps them as narrator lines.

## How to adapt
1. Find the dramatic spine: who wants what, what stands in the way, how it turns. Write a one-sentence
   logline and a synopsis of at most ~150 words.
2. Split the story into **3–8 scenes** (fewer for short sources; see Fidelity for `faithful`). Each scene has one location and a
   clear purpose. Merge or cut scenes that don't move the spine.
3. For each scene list **beats** — concrete, observable actions or reactions, one per line, in order.
   Turn thoughts and narration into something a camera can see (a gesture, an object, a look).
4. Keep only the dialogue that matters; lines must be short enough to subtitle (≤ 12 words).
5. Name characters and locations consistently; the next step builds a bible from these names.
6. Respect `settings` (length targets, language). Write in the language of the source unless
   `settings.language` says otherwise.

## Revisions
If `revision_request` is present, change what it asks and keep everything else from
`previous_output` stable (same scene ids where scenes survive).

## Ids
Scene ids are short lowercase slugs: `s1`, `s2`, … or `market`, `rooftop`.
