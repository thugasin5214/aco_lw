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
    """Run queued tool / skill jobs in order until the queue is empty (when brief finishes, adaptation is queued next)."""
    for _ in range(10):
        jobs = p.pending_skill_jobs()
        if not jobs:
            return
        for art in jobs:
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
    assert interp("{settings.n}", ctx) == 3  # a whole-string reference keeps its type
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
    # material intake is queued automatically (queued counts as running)
    assert stage(plan, "brief")["state"] == "active"
    assert stage(plan, "adaptation")["state"] == "blocked"

    run_skills(p)
    plan = p.plan(pr["id"])
    assert stage(plan, "adaptation")["state"] == "review"
    assert plan["next_action"] == {"type": "review", "stage": "adaptation"}
    # a skill's system prompt = SKILL.md + output contract; the user message carries the upstream
    # inputs
    sys_msg, user_msg = p.fake_llm.calls[0][1]
    assert "short-film treatment" in sys_msg["content"] and '"scenes"' in sys_msg["content"]
    brief = json.loads(user_msg["content"])["inputs"]["brief"]
    assert "Mei lost her cat" in brief["text"] and brief["references"] == []

    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    # → casting fans out automatically: 2 characters + 1 location
    p.review(pr["id"], "bible", "approve")
    plan = p.plan(pr["id"])
    chars = stage(plan, "characters")
    assert chars["state"] == "active" and [i["key"] for i in chars["items"]] == ["mei", "cat"]
    rounds = p.store.rounds(None, "generating")
    assert len(rounds) == 3 and all(r["n"] == 4 for r in rounds)  # settings.image_candidates
    t = p.store.round_tasks(next(r for r in rounds if r["title"].endswith("mei"))["id"])[0]
    assert "girl, red coat" in t["params"]["prompt"] and "35mm" in t["params"]["prompt"]

    select_all(p)  # picking approves → the storyboard runs automatically
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    plan = p.plan(pr["id"])
    assert stage(plan, "keyframes")["state"] == "active"
    kf = next(r for r in p.store.rounds(None, "generating") if r["title"].endswith("sh02"))
    mei_task = p._current(p._index(pr["id"]), "characters", "mei")["task_id"]
    ref = p.store.round_tasks(kf["id"])[0]["params"]["image"]
    assert mei_task in ref  # keyframes use the picked casting image as reference

    select_all(p)
    plan = p.plan(pr["id"])
    clips = stage(plan, "clips")
    assert clips["state"] == "ready" and clips["confirm"]  # video does not start automatically
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

    # change one character's look: only mei's casting image goes stale, cat and the location are
    # unaffected
    bible2 = json.loads(json.dumps(BIBLE))
    bible2["characters"][0]["look"] = "girl, yellow raincoat"
    p.review(pr["id"], "bible", "approve", data=bible2, note="raincoat")
    plan = p.plan(pr["id"])
    states = {i["key"]: i["state"] for i in stage(plan, "characters")["items"]}
    # stale items rerun automatically
    assert states["mei"] == "running" and states["cat"] == "current"
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
    p.review(pr["id"], "storyboard", "keep")  # the storyboard still applies: keep it
    plan = p.plan(pr["id"])
    kf = {i["key"]: i["state"] for i in stage(plan, "keyframes")["items"]}
    # only shots referencing mei go stale (and rerun automatically)
    assert kf == {"sh01": "current", "sh02": "running"}


def test_redo_round_follows_artifact_and_reject_skill(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "redo", note="make it funnier")
    msgs = p.fake_llm.calls  # redo: carries the previous output and the change request
    run_skills(p)
    user = json.loads(p.fake_llm.calls[-1][1][1]["content"])
    assert user["revision_request"] == "make it funnier" and user["previous_output"] == ADAPT
    assert len(msgs) == 2
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    ready = drain(p.studio)
    mei = next(r for r in ready if r["title"].endswith("mei"))
    # redo from the Review page / Telegram: the new round follows the same artifact
    new = p.studio.redo(mei["id"])
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
    p.review(pr["id"], "adaptation", "approve", data=ADAPT)  # approved after editing by hand
    assert stage(p.plan(pr["id"]), "adaptation")["state"] == "completed"


def test_park_keeps_job_queued(prod):
    p = prod
    p.fake_llm.park = True
    pr = p.create("novel-to-film", "x", {"source": "..."})
    p.run_skill_job(p.pending_skill_jobs()[0])  # brief: a tool, no LLM needed
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
    assert abs(probe_seconds(out) - 3.0) < 0.2  # 1.5 + 2 − 0.5 transition overlap


def test_pending_reviews_list_planning_stages(prod):
    pr = prod.create("novel-to-film", "Lost cat", {"source": "..."})
    assert prod.pending_reviews() == []  # still queued
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
    # merged, not overwritten
    assert prod.store.get_production(pr["id"])["settings"]["video_tier"] == "smoke"


def test_redo_of_approved_storyboard_keeps_downstream_until_new_version(prod):
    p = prod
    pr = _to_keyframes(p)
    board2 = json.loads(json.dumps(BOARD))
    board2["shots"][1]["frame"] = "陈师傅抬头"  # only sh02 changed
    p.fake_llm.out["storyboard"] = [board2]
    p.review(pr["id"], "storyboard", "redo", note="all shots in Mandarin")
    cur = p._current(p._index(pr["id"]), "storyboard")
    assert cur and cur["version"] == 1  # the old version stays valid until the new one is approved
    run_skills(p)
    user = json.loads(p.fake_llm.calls[-1][1][1]["content"])
    assert user["revision_request"] == "all shots in Mandarin" and user["previous_output"] == BOARD
    p.review(pr["id"], "storyboard", "approve")
    kf = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "keyframes")["items"]}
    # only the shot whose text changed is redone
    assert kf == {"sh01": "current", "sh02": "running"}


def test_plain_reject_of_a_production_round_re_renders(prod):
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    ready = drain(p.studio)
    mei = next(r for r in ready if r["title"].endswith("mei"))
    p.studio.reject(mei["id"])  # no note
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "characters")["items"]}
    # rerun automatically with new seeds
    assert items["mei"]["state"] == "running" and items["mei"]["version"] == 2
    cat = next(r for r in ready if r["title"].endswith("cat"))
    # with a note: wait for the planner to derive a new round, no rerun here
    p.studio.reject(cat["id"], "make it fluffier")
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
    assert fake.frees == 0  # cpu resource class: ComfyUI is not released


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


# ---- 3.1 multi-source input + your own casting images ----
class FakeHTTP:
    """A fake httpx.Client: returns a web page or an image by URL."""

    def __init__(self, pages):
        self.pages = pages

    def stream(self, method, url):
        import contextlib

        import httpx

        body, ctype = self.pages[str(url)]

        @contextlib.contextmanager
        def cm():
            r = httpx.Response(200, content=body, headers={"content-type": ctype},
                               request=httpx.Request("GET", url))
            yield r

        return cm()


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def extract_info(self, url, download=True):
        from pathlib import Path

        out = Path(self.opts["outtmpl"]).parent
        (out / "abc.zh-Hans.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\n<c>你好</c>\n\n00:01.000 --> 00:02.000\n你好\n世界\n")
        return {"title": "测试视频", "uploader": "chan", "duration": 2, "description": "简介",
                "chapters": [{"start_time": 0, "title": "开头"}], "thumbnail": "https://img/thumb.jpg"}


def test_ingest_text_image_url_youtube(tmp_path):
    from video_agent.ingest import Ingest, vtt_to_text

    img = tmp_path / "me.png"
    img.write_bytes(b"\x89PNG fake")
    html = ("<html><head><title>T</title><meta property='og:image' content='/og.jpg'></head><body><article>"
            + "<p>一个关于雨夜修表铺的故事。老人和小女孩。</p>" * 20 + "</article></body></html>").encode()
    http = FakeHTTP({
        "https://site/a": (html, "text/html; charset=utf-8"),
        "https://site/og.jpg": (b"jpg", "image/jpeg"),
        "https://img/thumb.jpg": (b"jpg", "image/jpeg"),
        "https://site/bad": (b"%PDF", "application/pdf"),
    })
    ing = Ingest(tmp_path / "w", lambda uid: str(img), http=http, ytdl=FakeYDL)
    out = ing({"text": "雨夜修表", "sources": [
        {"kind": "image", "upload": "u1", "label": "陈师傅", "role": "character", "use": "as_is"},
        {"url": "https://site/a"},
        {"url": "https://www.youtube.com/watch?v=abc"},
        {"url": "https://site/bad"},
    ]}, {"youtube_frames": 0})
    assert "雨夜修表" in out["text"] and "修表铺的故事" in out["text"]
    assert "Transcript:\n你好\n世界" in out["text"] and "开头" in out["text"]
    refs = {r["label"]: r for r in out["references"]}
    assert refs["陈师傅"]["use"] == "as_is" and refs["陈师傅"]["role"] == "character"
    assert len(out["references"]) == 3  # your image + the page's main image + the video thumbnail
    status = [s["status"] for s in out["sources"]]
    # a broken link is only marked failed, other sources unaffected
    assert status == ["ok", "ok", "ok", "ok", "failed"]
    assert vtt_to_text("WEBVTT\n\n1\n00:00.000 --> 00:01.000\na\na\n") == "a"


def test_provided_casting_photo_as_is_and_stylize(prod, tmp_path):
    p = prod
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"\x89PNG a")
    b.write_bytes(b"\x89PNG b")
    bible = json.loads(json.dumps(BIBLE))
    bible["characters"][0]["reference"] = "r1"  # mei: used as-is
    bible["characters"][1]["reference"] = "r2"  # cat: redrawn in the film's style
    p.fake_llm.out["bible"] = [bible]
    pr = p.create("novel-to-film", "x", {"source": {"text": "story", "sources": [
        {"kind": "image", "path": str(a), "label": "Mei", "role": "character", "use": "as_is"},
        {"kind": "image", "path": str(b), "label": "Cat", "role": "character"},
    ]}})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    items = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "characters")["items"]}
    # your photo is approved directly; the cat is redrawn in style
    assert items == {"mei": "current", "cat": "running"}
    cat_round = next(r for r in p.store.rounds(None, "generating") if r["title"].endswith("cat"))
    assert cat_round["template"] == "flux2-edit"
    mei = p._current(p._index(pr["id"]), "characters", "mei")
    assert mei["data"]["provided"] and mei["data"]["outputs"][0]["path"].endswith(".png")


def test_manual_provide_replaces_an_item_and_downstream_uses_it(prod, tmp_path):
    p = prod
    pr = _to_keyframes(p)
    img = tmp_path / "mine.png"
    img.write_bytes(b"\x89PNG mine")
    p.provide(pr["id"], "characters", "mei", str(img))
    kf = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "keyframes")["items"]}
    # shots referencing mei are redone with the new photo
    assert kf["sh02"] == "running" and kf["sh01"] == "current"
    rnd = next(r for r in p.store.rounds(None, "generating") if r["title"].endswith("sh02"))
    assert p.store.round_tasks(rnd["id"])[0]["params"]["image"].endswith(".png")


def test_done_production_reactivates_when_work_reappears(prod):
    p = prod
    pr = p.create("novel-to-film", "x", {"source": "..."})
    p.store.update_production(pr["id"], state="done")  # e.g. it ran to completion before
    p.run_stage(pr["id"], "brief") if p.pending_skill_jobs() == [] else None
    assert p.plan(pr["id"])["production"]["state"] == "active"
    run_skills(p)
    assert stage(p.plan(pr["id"]), "adaptation")["state"] == "review"


def test_pause_holds_queued_work_and_resume_continues(prod):
    p = prod
    pr = p.create("novel-to-film", "x", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")  # casting and locations are queued
    drafts = p.store.by_state("draft")
    assert drafts and not any(p.holds(t) for t in drafts)
    p.pause(pr["id"])
    assert all(p.holds(t) for t in drafts)  # the Worker skips them, the queue stays as it is
    assert p.store.by_state("draft") == drafts
    p.store.update_production(pr["id"], state="paused")
    # the plan does not flip it back to active
    assert p.plan(pr["id"])["production"]["state"] == "paused"
    p.fake_llm.out["adaptation"] = [ADAPT]
    # LLM jobs queued while paused do not run either
    p.review(pr["id"], "adaptation", "redo", note="x")
    assert p.pending_skill_jobs() == []
    p.resume(pr["id"])
    assert not any(p.holds(t) for t in p.store.by_state("draft"))
    assert len(p.pending_skill_jobs()) == 1


def test_pause_can_stop_the_running_task(prod):
    stopped = []
    prod.stop_running = lambda pid: stopped.append(pid) or {"id": "t1"}
    pr = prod.create("novel-to-film", "x", {"source": "..."})
    out = prod.pause(pr["id"], stop_running=True)
    assert stopped == [pr["id"]] and out["interrupted"] == "t1"
