import json
import shutil
import subprocess

import pytest

from video_agent.config import ROOT
from video_agent.executors.ffmpeg import build_command, probe_seconds, to_srt
from video_agent.production import (
    Park,
    Production,
    ProductionError,
    extract_json,
    interp,
    load_workflows,
)
from video_agent.skills import SkillLibrary
from video_agent.studio import Studio

from .test_studio import drain

ADAPT = {
    "logline": "A girl finds a lost cat.",
    "synopsis": "Mei searches the market and finds the cat on a rooftop.",
    "scenes": [
        {
            "id": "s1",
            "summary": "search",
            "location": "market",
            "characters": ["mei"],
            "beats": ["looks"],
        }
    ],
}
BIBLE = {
    "style": {"look": "35mm film still"},
    "characters": [
        {"id": "mei", "name": "Mei", "look": "girl, red coat"},
        {"id": "cat", "name": "Cat", "look": "ginger cat"},
    ],
    "locations": [{"id": "market", "name": "Market", "look": "night market"}],
}
BOARD = {
    "shots": [
        {
            "id": "sh01",
            "scene": "s1",
            "ref": "locations/market",
            "frame": "market at night",
            "framing": "wide",
            "camera": "static",
            "motion": "lanterns sway",
            "seconds": 3,
        },
        {
            "id": "sh02",
            "scene": "s1",
            "ref": "characters/mei",
            "frame": "Mei looks up",
            "framing": "close-up",
            "camera": "push-in",
            "motion": "she turns",
            "seconds": 3,
            "dialogue": "There you are.",
        },
    ]
}


class FakeLLM:
    def __init__(self):
        self.out = {"adaptation": [ADAPT], "bible": [BIBLE], "storyboard": [BOARD]}
        self.calls = []
        self.park = False

    def __call__(self, stage, messages, schema):
        if self.park:
            raise Park("video render running")
        self.calls.append((stage.id, messages))
        q = self.out[stage.id]
        v = q.pop(0) if len(q) > 1 else q[0]
        return (v if isinstance(v, str) else json.dumps(v)), {"via": "fake"}


@pytest.fixture
def prod(make_orch, fake, tmp_path):
    studio = Studio(make_orch(fake), round_size=2)
    llm = FakeLLM()
    p = Production(
        studio,
        SkillLibrary((ROOT / "skills", "builtin")),
        load_workflows(ROOT / "workflows"),
        llm,
        log=lambda m: None,
        work_dir=tmp_path / "productions",
    )
    p.fake_llm = llm
    return p


def run_skills(p: Production) -> None:
    for art in p.pending_skill_jobs():
        p.run_skill_job(art)


def stage(plan, sid):
    return next(s for s in plan["stages"] if s["id"] == sid)


def select_all(p: Production, which=1) -> None:
    for rnd in drain(p.studio):
        p.on_round_ready(rnd)
    for rnd in p.store.rounds(None, "review"):
        p.studio.select(rnd["id"], which)


def test_expressions_and_json_extraction():
    ctx = {"item": {"id": "a", "tags": ["x"]}, "settings": {"n": 3}}
    assert interp("{item.id}-{settings.n}", ctx) == "a-3"
    assert interp("{settings.n}", ctx) == 3  # 整串引用保留类型
    assert interp({"p": ["{item.tags.0}"]}, ctx) == {"p": ["x"]}
    assert extract_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('blah {"a": {"b": 2}} tail') == {"a": {"b": 2}}


def test_workflow_validation_rejects_bad_dag(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\ntitle: Bad\nstages:\n  - {id: a, kind: skill, skill: x, needs: [b], schema: {}}\n"
        "  - {id: b, kind: input}\n"
    )
    with pytest.raises(ValueError, match="排在后面"):
        load_workflows(tmp_path)


def test_full_pipeline_with_gates_and_confirm(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "Mei lost her cat..."})
    plan = p.plan(pr["id"])
    assert stage(plan, "source")["state"] == "completed"
    assert stage(plan, "adaptation")["state"] == "active"  # 自动排队（queued 记作 running）
    assert stage(plan, "bible")["state"] == "blocked"

    run_skills(p)
    plan = p.plan(pr["id"])
    assert stage(plan, "adaptation")["state"] == "review"
    assert plan["next_action"] == {"type": "review", "stage": "adaptation"}
    # skill 的系统提示词 = SKILL.md + 输出契约；用户消息带上游输入
    sys_msg, user_msg = p.fake_llm.calls[0][1]
    assert "short-film treatment" in sys_msg["content"] and '"scenes"' in sys_msg["content"]
    assert json.loads(user_msg["content"])["inputs"]["source"].startswith("Mei")

    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")  # → 自动展开定妆：2 个角色 + 1 个场景
    plan = p.plan(pr["id"])
    chars = stage(plan, "characters")
    assert chars["state"] == "active" and [i["key"] for i in chars["items"]] == ["mei", "cat"]
    rounds = p.store.rounds(None, "generating")
    assert len(rounds) == 3 and all(r["n"] == 4 for r in rounds)  # settings.image_candidates
    t = p.store.round_tasks(next(r for r in rounds if r["title"].endswith("mei"))["id"])[0]
    assert "girl, red coat" in t["params"]["prompt"] and "35mm" in t["params"]["prompt"]

    select_all(p)  # 选中即通过 → 自动跑分镜
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    plan = p.plan(pr["id"])
    assert stage(plan, "keyframes")["state"] == "active"
    kf = next(r for r in p.store.rounds(None, "generating") if r["title"].endswith("sh02"))
    mei_task = p._current(p._index(pr["id"]), "characters", "mei")["task_id"]
    ref = p.store.round_tasks(kf["id"])[0]["params"]["image"]
    assert mei_task in ref  # 关键帧以已选的定妆图为参考

    select_all(p)
    plan = p.plan(pr["id"])
    clips = stage(plan, "clips")
    assert clips["state"] == "ready" and clips["confirm"]  # 视频不自动开跑
    assert plan["next_action"] == {
        "type": "run_stage",
        "stage": "clips",
        "keys": ["sh01", "sh02"],
        "confirm": True,
    }
    p.run_stage(pr["id"], "clips")
    vids = p.store.rounds(None, "generating")
    assert len(vids) == 2 and all(r["template"] == "h3-i2v" and r["tier"] == "smoke" for r in vids)


def test_staleness_is_per_item(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    select_all(p)
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    select_all(p)

    # 改一个角色的外观：只让 mei 的定妆图过期，cat 与场景不受影响
    bible2 = json.loads(json.dumps(BIBLE))
    bible2["characters"][0]["look"] = "girl, yellow raincoat"
    p.review(pr["id"], "bible", "approve", data=bible2, note="raincoat")
    plan = p.plan(pr["id"])
    states = {i["key"]: i["state"] for i in stage(plan, "characters")["items"]}
    assert states["mei"] == "running" and states["cat"] == "current"  # 过期的自动重跑
    assert stage(plan, "locations")["state"] == "completed"
    select_all(p)
    plan = p.plan(pr["id"])
    assert stage(plan, "storyboard")["items"][0]["state"] == "stale"
    assert plan["next_action"] == {
        "type": "stale",
        "stage": "storyboard",
        "options": ["keep", "redo"],
    }
    assert stage(plan, "keyframes")["state"] == "blocked"
    p.review(pr["id"], "storyboard", "keep")  # 分镜仍然适用：保留
    plan = p.plan(pr["id"])
    kf = {i["key"]: i["state"] for i in stage(plan, "keyframes")["items"]}
    assert kf == {"sh01": "current", "sh02": "running"}  # 只有引用 mei 的镜头过期（并自动重跑）


def test_redo_round_follows_artifact_and_reject_skill(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "redo", note="make it funnier")
    msgs = p.fake_llm.calls  # 重做：带上一版输出和修改意见
    run_skills(p)
    user = json.loads(p.fake_llm.calls[-1][1][1]["content"])
    assert user["revision_request"] == "make it funnier" and user["previous_output"] == ADAPT
    assert len(msgs) == 2
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    ready = drain(p.studio)
    mei = next(r for r in ready if r["title"].endswith("mei"))
    new = p.studio.redo(mei["id"])  # 在 Review 页 / Telegram 上重做：新轮自动跟随到同一产物
    art = p.store.artifact_by_round(new["id"])
    assert art and art["stage"] == "characters" and art["key"] == "mei" and art["version"] == 2
    drain(p.studio)
    p.studio.select(new["id"], 2)
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "characters")["items"]}
    assert items["mei"]["state"] == "current" and items["mei"]["current_version"] == 2


def test_invalid_output_gets_one_repair_then_invalid(prod):
    p = prod
    p.fake_llm.out["adaptation"] = ["not json", '{"logline": "x"}', ADAPT]
    pr = p.create("novel-to-film", "x", {"source": "..."})
    run_skills(p)
    a = p.plan(pr["id"])
    item = stage(a, "adaptation")["items"][0]
    assert item["state"] == "invalid"
    with pytest.raises(ProductionError, match="schema"):
        p.review(pr["id"], "adaptation", "approve")
    p.review(pr["id"], "adaptation", "approve", data=ADAPT)  # 手改后通过
    assert stage(p.plan(pr["id"]), "adaptation")["state"] == "completed"


def test_park_keeps_job_queued(prod):
    p = prod
    p.fake_llm.park = True
    pr = p.create("novel-to-film", "x", {"source": "..."})
    with pytest.raises(Park):
        p.run_skill_job(p.pending_skill_jobs()[0])
    assert p.store.artifacts(pr["id"], "adaptation")[0]["status"] == "queued"
    p.fake_llm.park = False
    run_skills(p)
    assert stage(p.plan(pr["id"]), "adaptation")["state"] == "review"


def test_blocked_stage_cannot_run(prod):
    pr = prod.create("novel-to-film", "x", {"source": "..."})
    with pytest.raises(ProductionError, match="上游"):
        prod.run_stage(pr["id"], "keyframes")


def test_materialize_and_ffmpeg_command(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    files = []
    for i, color in enumerate(["red", "blue"]):
        f = tmp_path / f"c{i}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:s=320x240:d=2:r=24",
                "-pix_fmt",
                "yuv420p",
                str(f),
            ],
            check=True,
        )
        files.append(f)
    arts = {
        f"sh{i}": {"version": 1, "data": {"outputs": [{"kind": "video", "path": str(f)}]}}
        for i, f in enumerate(files)
    }
    tl = Production.materialize(
        {
            "clips": [{"shot": "sh0", "in": 0.5, "subtitle": "hi"}, {"shot": "sh1"}],
            "transition": {"type": "fade", "seconds": 0.5},
        },
        arts,
        {"width": 320, "height": 240, "fps": 24},
    )
    assert tl["subtitles"] == [{"start": 0.0, "end": 1.5, "text": "hi"}]
    for c in tl["clips"]:
        c["_len"], c["_audio"] = c["out"] - c["in"], False
    srt = tmp_path / "s.srt"
    srt.write_text(to_srt(tl["subtitles"]))
    out = tmp_path / "final.mp4"
    subprocess.run(build_command(tl, out, srt), check=True)
    assert abs(probe_seconds(out) - 3.0) < 0.2  # 1.5 + 2 − 0.5 转场重叠


def test_pending_reviews_list_planning_stages(prod):
    pr = prod.create("novel-to-film", "Lost cat", {"source": "..."})
    assert prod.pending_reviews() == []  # 还在排队
    run_skills(prod)
    (r,) = prod.pending_reviews()
    assert (r["production_id"], r["stage"], r["state"]) == (pr["id"], "adaptation", "review")
    assert r["data"]["logline"] == ADAPT["logline"]
    prod.review(pr["id"], "adaptation", "approve")
    assert [x["stage"] for x in prod.pending_reviews()] == []


def _to_keyframes(p):
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."}, {"language": "Mandarin Chinese"})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    select_all(p)
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    select_all(p)
    return pr


def test_language_reaches_video_prompts(prod):
    pr = _to_keyframes(prod)
    prod.run_stage(pr["id"], "clips")
    (t,) = prod.store.round_tasks(prod.store.rounds(None, "generating")[0]["id"])
    assert t["params"]["prompt"].endswith("Dialogue and speech in Mandarin Chinese.")
    prod.update_settings(pr["id"], {"language": ""})
    assert prod.store.get_production(pr["id"])["settings"]["video_tier"] == "smoke"  # 合并，不覆盖


def test_redo_of_approved_storyboard_keeps_downstream_until_new_version(prod):
    p = prod
    pr = _to_keyframes(p)
    board2 = json.loads(json.dumps(BOARD))
    board2["shots"][1]["frame"] = "陈师傅抬头"  # 只改 sh02
    p.fake_llm.out["storyboard"] = [board2]
    p.review(pr["id"], "storyboard", "redo", note="all shots in Mandarin")
    cur = p._current(p._index(pr["id"]), "storyboard")
    assert cur and cur["version"] == 1  # 旧版本在新版本通过前仍然有效
    run_skills(p)
    user = json.loads(p.fake_llm.calls[-1][1][1]["content"])
    assert user["revision_request"] == "all shots in Mandarin" and user["previous_output"] == BOARD
    p.review(pr["id"], "storyboard", "approve")
    kf = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "keyframes")["items"]}
    assert kf == {"sh01": "current", "sh02": "running"}  # 只有文字变了的镜头重出


def test_plain_reject_of_a_production_round_re_renders(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    ready = drain(p.studio)
    mei = next(r for r in ready if r["title"].endswith("mei"))
    p.studio.reject(mei["id"])  # 不带意见
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "characters")["items"]}
    assert items["mei"]["state"] == "running" and items["mei"]["version"] == 2  # 自动换种子重出
    cat = next(r for r in ready if r["title"].endswith("cat"))
    p.studio.reject(cat["id"], "make it fluffier")  # 带意见：等规划器派生新轮，不在这里重出
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "characters")["items"]}
    assert items["cat"]["state"] == "rejected"


def test_manual_rerun_of_an_approved_item_keeps_it_until_replaced(prod):
    p = prod
    pr = _to_keyframes(p)
    old = p._current(p._index(pr["id"]), "keyframes", "sh01")
    p.run_stage(pr["id"], "keyframes", ["sh01"], "manual re-run")
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "keyframes")["items"]}
    assert items["sh01"]["state"] == "running" and items["sh01"]["current_version"] == old["version"]
    with pytest.raises(ProductionError, match="正在生成"):
        p.run_stage(pr["id"], "keyframes", ["sh01"])
    select_all(p)
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "keyframes")["items"]}
    assert items["sh01"]["state"] == "current" and items["sh01"]["current_version"] == old["version"] + 1


def test_ffmpeg_assemble_through_orchestrator_collects_outputs(make_orch, fake, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    clip = tmp_path / "c.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=24",
         "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    tl = tmp_path / "tl.json"
    tl.write_text(json.dumps({"width": 320, "height": 240, "clips": [{"file": str(clip)}, {"file": str(clip)}],
                              "subtitles": [{"start": 0, "end": 1, "text": "你好"}]}))
    orch = make_orch(fake)
    orch.sleep = lambda s: __import__("time").sleep(0.05)
    task = orch.create("ffmpeg-assemble", {"timeline": str(tl), "subtitles": True})
    done = orch.execute(task["id"])
    assert done["state"] == "review", done
    assert {o["file"] for o in done["outputs"]} == {"final.mp4", "final.srt"}
    assert abs(probe_seconds(orch.store.task_dir(task["id"]) / "final.mp4") - 2.0) < 0.2
    assert fake.frees == 0  # cpu 资源类：不释放 ComfyUI


def test_executor_success_without_outputs_fails(make_orch, fake):
    orch = make_orch(fake)
    ex = orch.executors["comfy"]
    ex.collect = lambda *a, **k: []
    task = orch.create("flux2-t2i", {"prompt": "x"})
    done = orch.execute(task["id"])
    assert done["state"] == "failed" and "no output" in done["error"]


@pytest.mark.parametrize("transition", [None, {"type": "fade", "seconds": 0.3}])
def test_assemble_keeps_clip_audio(tmp_path, transition):
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    voiced, silent = tmp_path / "v.mp4", tmp_path / "s.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=1", "-shortest",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(voiced)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=blue:s=320x240:d=1:r=24",
         "-pix_fmt", "yuv420p", str(silent)],
        check=True,
    )
    from video_agent.executors.ffmpeg import has_audio

    clips = [{"file": str(f), "in": 0, "out": 1.0, "_len": 1.0, "_audio": has_audio(f)} for f in (voiced, silent)]
    assert [c["_audio"] for c in clips] == [True, False]
    out = tmp_path / "o.mp4"
    tl = {"width": 320, "height": 240, "clips": clips, "transition": transition}
    subprocess.run(build_command(tl, out, None), check=True)
    assert has_audio(out)
    assert abs(probe_seconds(out) - (2.0 - (0.3 if transition else 0))) < 0.15
