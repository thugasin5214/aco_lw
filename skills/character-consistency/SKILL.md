---
name: character-consistency
description: Keep one person's face the same across images and videos — casting photo or character LoRA → identity images → video that does not drift. Use whenever the user gives a casting photo / names a LoRA / says "same person", "lock this face", "用这个定妆", or a video drifted off the character.
---

# Character consistency

A video model only sees the person through what you give it. Image-to-video (`h3-i2v`) sees the
face **once, in the first frame**; every later frame is the model's guess, so the longer the clip
and the bigger the motion, the further it drifts. Lock the identity at every step instead.

## 1. Identity source
- **A character LoRA** (listed under "LoRAs installed in ComfyUI" in your prompt) is the strongest
  lock for images. A LoRA works only with its own base model: Klein 4B LoRAs go into
  `flux2-lora-t2i` / `flux2-lora-edit` (`params.lora`, `params.lora_strength`). They cannot be
  loaded into the H3 video model — they reach the video through the images you make with them.
- Strength: use the `strength` noted for that LoRA; otherwise 0.45–0.6. Higher locks the face
  harder but freezes pose, light and style; lower drifts.
- Several LoRAs of the same person in different styles (photo / anime): pick the style the user
  wants; don't stack them.
- **Only a casting photo, no LoRA**: use it as the reference image (`flux2-edit`, `zimage-i2i`) and
  as the video reference below.
- If the project memory does not yet say which LoRA belongs to which character, `propose_memory`
  it once the user confirms (e.g. "heroine = <lora file>, strength 0.5, photo style").

## 2. Identity images (cheap, seconds each)
With `flux2-lora-edit` (casting photo as `image`, LoRA on) make what the video needs:
- a clean **front portrait** (face reference for the whole clip);
- the **first frame of each shot** in its pose, outfit and framing.
`look` at them next to the casting photo; redo the ones that miss before spending minutes on video.

## 3. Video that keeps the face
| Need | Route |
|---|---|
| One clip, face must hold the whole time | `freevideo-ref` **without `ref_video`**: `ref_image` = front portrait, `ref_image2` = the shot's first frame; prompt names them `<Picture 1>` / `<Picture 2>`. Start at `draft-5s`. |
| Short clip (≤ 10 s), little head turning | `h3-i2v` with the LoRA first frame as `image`. |
| Long action (e.g. a 60 s timeline) | Don't render one long i2v clip. Split it into 5–10 s shots, each with its own LoRA first frame (or `freevideo-ref`), then join them with `ffmpeg-assemble`. |
| Multi-shot film, several characters | FreeVideo cast card (story-to-film workflow or `delegate_freevideo`) with the LoRA images as the card's pictures. |
| Copy a reference video's moves with this person | `freevideo-ref` with `ref_video` + the LoRA portrait as `ref_image` (see the video replication rules). |

`h3n-r2v` (native 4-reference) needs CUDA: on Apple silicon it fails (`aten::_int_mm` missing on MPS), so don't use it on a Mac.

## 4. Check before reporting
`look` at the video frames against the casting photo: face shape, eyes, hair, age. If it drifted,
say where (e.g. "after the head turn at ~4 s"), then shorten the shot, add the portrait reference,
or raise the LoRA strength for the first frame — don't hand the user a clip with the wrong face.
