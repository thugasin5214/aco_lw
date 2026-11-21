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
    assert "speed.safetensors — base minimax-h3; no template takes it" in text
    assert "broken.safetensors — base unknown" in text
    assert cat.text() is text  # cached: the prompt prefix stays byte-identical
    assert read_meta(loras / "broken.safetensors") == {} and base_of({}) == ""
