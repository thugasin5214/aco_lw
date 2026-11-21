"""LoRA templates and the LoRA catalog (agent-side tests live in test_agent.py)."""

import json
import struct

import pytest

from video_agent.assets import LoraCatalog, base_of, read_meta
from video_agent.render import ParamError, render, resolve


def write_lora(path, meta):
    head = json.dumps({"__metadata__": meta}).encode()
    path.write_bytes(struct.pack("<Q", len(head)) + head)


def test_lora_templates_insert_the_lora_between_model_and_guider(make_orch, fake):
    reg = make_orch(fake).registry
    for name in ("flux2-lora-t2i", "flux2-lora-edit"):
        t = reg.template(name)
        params = {"prompt": "her", "lora": "x.safetensors", "lora_strength": 0.5}
        if name == "flux2-lora-edit":
            params["image"] = "ref.png"
        _, p = resolve(t, params, None, allow_unverified=True)
        g = render(t, p)
        assert g["50"]["inputs"] == {
            "model": ["1", 0],
            "lora_name": "x.safetensors",
            "strength_model": 0.5,
        }
        assert g["9"]["inputs"]["model"] == ["50", 0]
        with pytest.raises(ParamError, match="lora"):
            resolve(
                t, {k: v for k, v in params.items() if k != "lora"}, None, allow_unverified=True
            )


def test_catalog_reads_base_model_and_personal_notes(make_orch, fake, tmp_path):
    orch = make_orch(fake)
    comfy = tmp_path / "ComfyUI"
    loras = comfy / "models" / "loras"
    loras.mkdir(parents=True)
    write_lora(loras / "face.safetensors", {"ss_base_model_version": "flux_2_klein_4b"})
    write_lora(loras / "speed.safetensors", {"base_model": "Comfy-Org/MiniMax-H3 x"})
    (loras / "broken.safetensors").write_bytes(b"\xff" * 4)
    orch.cfg.comfy_output_dir = str(comfy / "output")
    (orch.cfg.data_dir / "loras.yaml").write_text(
        "face.safetensors:\n  person: heroine\n  strength: 0.5\n"
    )
    cat = LoraCatalog(orch.cfg, orch.registry)
    text = cat.text()
    assert (
        "face.safetensors — base flux2-klein-4b; use with flux2-lora-edit, flux2-lora-t2i" in text
    )
    assert "person: heroine; strength: 0.5" in text
    assert "speed.safetensors — base minimax-h3; use with h3-lora-ref" in text
    assert "broken.safetensors — base unknown" in text
    assert cat.text() is text  # cached: the prompt prefix stays byte-identical
    assert read_meta(loras / "broken.safetensors") == {} and base_of({}) == ""


def test_h3_lora_template_stacks_three_loras_onto_a_reference(make_orch, fake):
    t = make_orch(fake).registry.template("h3-lora-ref")
    params = {"prompt": "crash_zm, she dances", "image": "ref.png", "lora": "zoom.safetensors"}
    _, p = resolve(
        t,
        {**params, "lora2": "dance.safetensors", "lora2_strength": 0.7},
        None,
        allow_unverified=True,
    )
    g = render(t, p)
    assert g["3"]["inputs"]["references"] == ["5", 0] and "first" not in g["3"]["inputs"]
    assert g["5"]["inputs"]["image"] == ["1", 0]
    assert g["3"]["inputs"]["loras"] == ["8", 0]
    assert (g["6"]["inputs"]["lora"], g["7"]["inputs"]["lora"], g["8"]["inputs"]["lora"]) == (
        "zoom.safetensors",
        "dance.safetensors",
        "None",
    )
    assert g["7"]["inputs"]["strength"] == 0.7 and g["8"]["inputs"]["previous"] == ["7", 0]
    with pytest.raises(ParamError, match="lora"):
        resolve(t, {k: v for k, v in params.items() if k != "lora"}, None, allow_unverified=True)


def test_notes_give_the_base_model_when_the_file_has_none(make_orch, fake, tmp_path):
    orch = make_orch(fake)
    loras = tmp_path / "ComfyUI" / "models" / "loras"
    loras.mkdir(parents=True)
    write_lora(loras / "zoom.safetensors", {})
    orch.cfg.comfy_output_dir = str(tmp_path / "ComfyUI" / "output")
    (orch.cfg.data_dir / "loras.yaml").write_text(
        "zoom.safetensors:\n  base: minimax-h3\n  kind: camera\n  trigger: crash_zm\n"
    )
    text = LoraCatalog(orch.cfg, orch.registry).text()
    assert "zoom.safetensors — base minimax-h3; use with h3-lora-ref" in text
    assert "kind: camera" in text and "trigger: crash_zm" in text


def test_h3_lora_face_reference_is_optional(make_orch, fake):
    t = make_orch(fake).registry.template("h3-lora-ref")
    base = {"prompt": "p", "image": "body.png", "lora": "zoom.safetensors"}
    _, p = resolve(t, base, None, allow_unverified=True)
    g = render(t, p)
    assert "2" not in g and "9" not in g and "previous" not in g["5"]["inputs"]
    _, p = resolve(t, {**base, "image2": "face.png"}, None, allow_unverified=True)
    g = render(t, p)
    assert g["2"]["inputs"]["image"] == "face.png" and g["5"]["inputs"]["previous"] == ["9", 0]
