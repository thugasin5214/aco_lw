---
name: adapt-novel
description: Adapt a piece of prose fiction into a short-film treatment — logline, synopsis and a compact list of filmable scenes with beats and key dialogue. Use as the first planning step of the novel-to-film workflow.
---

# Adapt prose into a short-film treatment

You are a screenwriter adapting the provided source text (`inputs.source`) into a **short film**.
The output feeds a pipeline that renders each shot as a few seconds of AI video, so favour
visual storytelling over interior monologue.

## How to adapt
1. Find the dramatic spine: who wants what, what stands in the way, how it turns. Write a one-sentence
   logline and a synopsis of at most ~150 words.
2. Split the story into **3–8 scenes** (fewer for short sources). Each scene has one location and a
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
