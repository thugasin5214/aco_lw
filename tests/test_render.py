import json
from pathlib import Path

import pytest

from video_agent.config import ROOT
from video_agent.registry import Registry
from video_agent.render import ParamError, check_graph, params_hash, render, resolve

REG = Registry(ROOT / "templates")
OBJECT_INFO = json.loads((Path(__file__).parent / "object_info_min.json").read_text())


def test_all_templates_load_and_pass_object_info():
    for t in REG.templates.values():
        _, p = resolve(
            t, {"image": "up.png"} if "image" in t.slots else {}, allow_unverified=True
        )
        assert check_graph(render(t, p), OBJECT_INFO) == [], t.name


def test_tier_fills_size():
    tier, p = resolve(REG.template("flux2-t2i"), {"seed": 7})
    assert tier == "standard" and (p["width"], p["height"], p["seed"]) == (1280, 720, 7)


def test_render_writes_slots_and_prefix():
    t = REG.template("flux2-t2i")
    _, p = resolve(t, {"prompt": "猫", "seed": 3})
    g = render(t, p, output_prefix="aco_lw/x")
    assert g["7"]["inputs"]["text"] == "猫"
    assert g["10"]["inputs"]["noise_seed"] == 3
    assert g["20"]["inputs"]["value"] == 1280
    assert g["16"]["inputs"]["filename_prefix"] == "aco_lw/x"
    assert t.graph["7"]["inputs"]["text"] != "猫"  # 模板本身不被修改


@pytest.mark.parametrize(
    "params, msg",
    [
        ({"bogus": 1}, "未知参数"),
        ({"width": 1024}, "由档位决定"),
        ({"steps": 99}, "超过上限"),
        ({"seed": "abc"}, "需要数字"),
        ({"seed": 1.5}, "需要整数"),
    ],
)
def test_resolve_rejects(params, msg):
    with pytest.raises(ParamError, match=msg):
        resolve(REG.template("flux2-t2i"), params)


def test_unverified_tier_needs_flag():
    with pytest.raises(ParamError, match="尚未实测"):
        resolve(REG.template("h3-t2v"), {}, tier="hd")
    assert resolve(REG.template("h3-t2v"), {}, tier="hd", allow_unverified=True)[1]["width"] == 1280


def test_required_image():
    with pytest.raises(ParamError, match="缺少必填参数 image"):
        resolve(REG.template("h3-i2v"), {}, allow_unverified=True)


def test_check_graph_catches_bad_values():
    t = REG.template("flux2-t2i")
    g = render(t, resolve(t, {})[1])
    g["1"]["inputs"]["unet_name"] = "missing.gguf"
    g["99"] = {"class_type": "NoSuchNode", "inputs": {}}
    problems = check_graph(g, OBJECT_INFO)
    assert any("不在可选值中" in p for p in problems)
    assert any("未知类型" in p for p in problems)


def test_params_hash_stable_and_sensitive():
    t = REG.template("flux2-t2i")
    _, a = resolve(t, {"seed": 1})
    _, b = resolve(t, {"seed": 2})
    assert params_hash(t, a) == params_hash(t, dict(a)) != params_hash(t, b)
