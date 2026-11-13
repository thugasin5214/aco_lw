# G0 baseline measurements (2026-10-26)

[中文](baseline.zh-CN.md)

Machine: Apple M4 Max, 128 GiB unified memory, VM running (the everyday state). Swap in use before measuring: 12.4 / 14 GiB.
Raw data: `data/bench/*` (gitignored); scripts: `bench/`. Every number below is **measured**, from a single run or 3 runs.

## LLM (LM Studio, qwen3.8-27b-splash, 4-bit, ctx 131072)

| Item | Measured |
|---|---|
| Unload | 0.3 s |
| Load | 15.4 s (3 runs: 15.4 / 15.4 / 15.4) |
| First token (just loaded / warm) | 0.3 s / 0.3 s |
| Footprint (available memory gained after unloading) | 19.3–19.9 GiB |
| Idle auto-unload with `lms load --ttl` | Works (ttl=20 s, unloaded after 45 s) |

> The design doc's estimate of "a minute or two to cold-start" does not hold: loading takes about 15 s. The 85 s in the design doc was OpenClaw's multi-turn agent reasoning time, not load time.

## Images (template 01, FLUX.2 Klein 4B Q8, 1280×720, 4 steps)

| Item | Measured |
|---|---|
| Cold start (including model load) | 23.0 s |
| Warm | 11.4 s |
| ComfyUI process peak RSS | 15.7 GiB (cold) / 7.8 GiB (warm) |
| System minimum available | 40.4 GiB; no memory pressure, no swap growth |

## ComfyUI `/free`

Called after the queue was confirmed empty twice: ComfyUI RSS 7.82 → 0.84 GiB, system available 46.7 → 54.3 GiB (+7.6 GiB); the endpoint returns immediately.

## Video (template 04, FreeVideo / MiniMax H3, 640×384, 3 s = 73 frames, two_pass 8+3 steps)

| Item | T0.5: 27B loaded | T0.6: 27B unloaded |
|---|---|---|
| Total time | 321 s | 230 s ※ |
| Sampling time | 209 s | 199 s |
| FreeVideo peak footprint (phys_footprint) | 53.4 GiB | 54.2 GiB |
| System minimum available | **16.2 GiB** | 36.9 GiB |
| Memory pressure (1 normal / 2 warning / 4 critical) | **2** | 1 |
| Swap change | **+3.7 GiB** | 0 |
| FreeVideo low_memory mode | No | No |

※ T0.6 hit the text-encoding cache (conditioning_cache_hit), so total times are not directly comparable; sampling time is: a resident 27B only slows sampling by about 5%.

## Conclusions → stage 1 defaults

1. **Video tier**: 640×384×3 s runs in the everyday state; FreeVideo peaks at about 54 GiB whether or not the 27B is loaded (it does not shrink its footprint because the LLM is there). Measure again before raising the tier.
2. **The LLM and video can coexist, but memory pressure reaches the warning level and swap grows** → default policy: **unload the LLM before submitting video** (the cost is only a ~15 s reload) and restore it per the residency mode afterwards. Image tasks need no unloading.
3. **Waking on demand is cheap**: cold start is about 15 s. The idle timer defaults to **5 min** (implemented with `--ttl`, verified to work); no "session residency" is needed to avoid reloads.
4. **Keep the generation model loaded during an image review session**: warm is twice as fast as cold (11 s vs 23 s) and takes about 8 GiB, so it can stay; `/free` before switching to video.
5. **Memory gate metric**: the ComfyUI process RSS does not show Metal usage (only 2–4 GiB during video), so it cannot be used. The gate uses **system available memory + memory pressure level** instead; model peaks are written to the model registry from FreeVideo's self-reported `ram_peak_bytes`.
6. Initial video gate threshold: available ≥ 54 GiB (peak) + 8 GiB margin ≈ **62 GiB**; otherwise unload the LLM / `/free` first; if still short, queue and wait.

## Follow-up: image-to-video (template 03 / h3-i2v, 2026-10-26, submitted via the orchestrator)

G1 casting image as the first frame, 640×384×3 s. Per policy the gate unloaded the 27B first (available 65.8 GiB ≥ 62); afterwards `/free`, and the 27B was restored with a 5 min TTL.

| Item | Measured |
|---|---|
| Execution / sampling | 329 s / 223 s |
| FreeVideo peak footprint | 54.9 GiB |
| System minimum available / pressure / swap | 21.7 GiB / **2 (warning)** / +0.04 GiB |

> Even with the LLM unloaded, I2V briefly reaches the warning level (about 1 GiB more peak than T2V), with almost no swap. The 62 GiB threshold stays; if everyday available memory drops further, check the VM and other resident processes first. Character consistency looks good by eye (first / middle / last frames).

## Follow-up: LM Studio prefix cache (2026-10-26, qwen3.8-27b-splash)

17.4k-token system prefix + different short questions, time to first token on streaming requests:

| Request | First token | Note |
|---|---|---|
| Cold start (including ~15 s model load) | 63.1 s | reading the context ~48 s ≈ 360 tokens/s |
| Same prefix, different question (×2) | 0.46 s / 0.45 s | prefix cache hit |
| One character changed at the start of the prefix | 47.7 s | everything recomputed |
| Back to the original prefix | 0.32 s | the engine keeps several caches |

> Conclusion: context must be "stable prefix + append only", with volatile content last; unloading the model invalidates the cache, so prewarm after reloading. See PLAN stage 2.5 / S8.

### Addendum: cache behaviour in multi-turn sessions (2026-10-26)

This model's (`qwen3_5` architecture, with linear-attention layers) prefix cache **can only be extended, not reused after truncation**:

| Request | First token |
|---|---|
| New request = old request + appended messages | 0.5 s (reused) |
| Only the system message identical, what follows changed | 4.8 s (not reused) |
| Resending any old request | 0.3 s (the engine keeps several snapshots) |

> So session history must **reproduce verbatim** the user messages sent at the time (including the live status of that moment). Implemented this way, a real three-turn conversation reuses about 55–73% of the prefix, first token 2–3 s; prewarming (resending the last request) takes 0.37 s.
> Turning thinking off: `reasoning_effort=low` and `chat_template_kwargs.enable_thinking=false` have no effect; the `/no_think` soft switch goes 14.4 s → 9.3 s (output 806 → 617 tokens).

### Addendum: caching in agent mode (native tool calling) (2026-10-26)

- Native tool calling works with LM Studio + qwen3.8-27b; the model issues several calls in parallel within one turn.
- **Within one run**: after replaying the model's thinking (`reasoning_content`), step 2 only computes the new part: ttft 2.04 s → 1.27 s (about 160 new tokens); real multi-step runs reuse about 74–92%.
- **Across runs**: the chat template drops earlier turns' thinking once a new user message arrives, so verbatim extension is impossible. Approach: fold finished runs into two stable messages (your words + final reply), and after each run prewarm the position "before the next message". Measured: the first call of a new run reuses about 40–55% (about 0% without prewarming).
- The engine's checkpoint rules are more complex than "before the last message" (reuse does not always align with message boundaries); further optimisation has limited payoff: most of each turn's time is the model's output (thinking + reply, about 8–15 s per call).

## Follow-up: hd-60s image-to-video (h3-i2v, 1280×736×60 s, 2026-10-26 22:28 → 2026-10-27 03:43)

| Item | Measured |
|---|---|
| Execution / sampling | 18 908 s (5.25 h) / 15 283 s |
| Frames | 1450 (60.4 s) |
| FreeVideo peak footprint | 98.4 GiB (low_memory not triggered) |
| Meanwhile | system available dropped to 13.9 GiB at one point, swap 34–36 GB (for part of the time the 27B had been loaded by mistake by a dev test; fixed with cross-process gating) |

> FreeVideo adapts to available memory; 98.4 GiB means "used because it was there", not the minimum need: the model registry marks it `adaptive: true` and admission still uses 54 GiB; peaks are tracked per tier so a big tier does not raise a small tier's threshold. The watch limit now scales with the expected duration (the old fixed 90 min marked this task as timed out; reconciliation collected it later).
