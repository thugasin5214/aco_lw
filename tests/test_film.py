import json

import pytest

from video_agent.config import ROOT
from video_agent.production import Production, load_workflows
from video_agent.skills import SkillLibrary
from video_agent.studio import Studio

from .test_production import FakeLLM, run_skills, select_all, stage

BOARD = {"shots": [
    {"id": "sh01", "scene": "s1", "cast": ["mei", "cat"], "frame": "Mei at the gate", "framing": "wide",
     "camera": "slow push in", "motion": "she looks around", "seconds": 5,
     "lines": [{"who": "mei", "line": "So this is it.", "how": "dry"}]},
    {"id": "sh02", "scene": "s1", "cast": ["mei"], "frame": "close on Mei", "framing": "close-up",
     "camera": "static shot", "motion": "she smiles", "seconds": 10, "continue": True},
]}


class FakeFV:
    def __init__(self):
        self.library = [{"id": "c_cat", "name": "Cat"}]
        self.created, self.started, self.chosen, self.reshot, self.jobs = [], [], [], [], {}
        self.film_state = {"id": "d000000000001", "shots": []}

    def cards(self):
        return self.library

    def create_card(self, name, description, images, kind="character"):
        self.created.append((name, description, images))
        return {"id": f"c_{name.lower()}"}

    def plan_film(self, sp, takes=1):
        return {"preview": {"shots": [{"id": s["id"], "prompt": f"P {s['id']}", "cast_reference": "cards"} for s in sp["shots"]]}}

    def start_film(self, sp, takes=1, ref=None):
        self.started.append((sp, takes))
        self.film_state["shots"] = [{"id": s["id"], "takes": []} for s in sp["shots"]]
        return {"id": "d000000000001"}

    def film(self, film_id):
        return self.film_state

    def call(self, method, path, body=None, **params):
        jid = path.rsplit("/", 1)[1]
        if jid not in self.jobs:
            raise AssertionError(f"looked up {jid}: not a job id")
        return {"job": self.jobs[jid]}

    def download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4")
        return dest

    def choose(self, film, shot, take, revision=None):
        self.chosen.append((shot, take))

    def reshoot(self, film, shot, note=None, takes=None):
        self.reshot.append((shot, note, takes))

    def cancel_film(self, film):
        self.cancelled = film

    def finish_takes(self, shot, n=2, status="succeeded"):
        sh = next(s for s in self.film_state["shots"] if s["id"] == shot)
        start = len(sh["takes"])
        for i in range(start + 1, start + n + 1):
            jid, ref = f"j{shot}{i:02d}", f"d1:{shot}:t{i}"  # 真实结构：job 是幂等键，task 才是任务 ID
            sh["takes"].append({"take": i, "job": ref, "task": jid, "state": "done"})
            self.jobs[jid] = {"status": status, "result": {"video": {"url": f"/v/{jid}.mp4"}}}


@pytest.fixture
def film_prod(make_orch, fake, tmp_path):
    studio = Studio(make_orch(fake), round_size=2)
    llm = FakeLLM()
    llm.out["storyboard"] = [BOARD]
    fv = FakeFV()
    p = Production(studio, SkillLibrary((ROOT / "skills", "builtin")), load_workflows(ROOT / "workflows"),
                   llm, log=lambda m: None, work_dir=tmp_path / "productions", freevideo=fv)
    p.fake_llm, p.fv = llm, fv
    return p


def to_film(p):
    pr = p.create("story-to-film", "Lost cat", {"source": "Mei lost her cat"})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    select_all(p)  # 定妆 → 角色卡（tool，自动）→ 分镜
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    return pr


def test_cards_reuse_library_and_create_from_sheets(film_prod):
    p = film_prod
    pr = to_film(p)
    cards = p._current(p._index(pr["id"]), "cards")["data"]
    assert cards["characters"] == {"mei": "c_mei", "cat": "c_cat"}  # 猫按名字复用卡库里的卡
    (name, desc, images), = p.fv.created
    assert name == "Mei" and desc == "girl, red coat" and images[0][1] == "sheet"
    plan = p.plan(pr["id"])
    assert stage(plan, "clips")["state"] == "ready" and stage(plan, "clips")["confirm"]


def test_film_screenplay_takes_review_and_choose(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")  # 你点了开始
    (sp, takes), = p.fv.started
    assert takes == 2 and sp["quality"] == "medium" and sp["characters"]["mei"] == {"card": "c_mei", "language": "English"}
    sh1, sh2 = sp["shots"]
    assert sh1["cast"] == ["mei", "cat"] and sh1["lines"][0]["who"] == "mei" and "continue" not in sh1
    assert sh2["continue"] is True and sh2["seconds"] == 10
    assert p.film.tick() == 0  # 还没有 take
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1
    items = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items == {"sh01": "review", "sh02": "running"}
    rnd = next(r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take")
    tasks = p.store.round_tasks(rnd["id"])
    assert [t["params"]["take"] for t in tasks] == [1, 2] and all(t["outputs"] for t in tasks)
    assert not any(p.holds(t) is False for t in tasks if t["state"] == "draft")
    p.studio.select(rnd["id"], 2)
    assert p.fv.chosen == [("sh01", 2)]  # 下一镜从这条续拍
    assert p.film.tick() == 0  # 已导入的 take 不会重复导入


def test_redo_and_reject_become_reshoots(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.fv.finish_takes("sh01")
    p.film.tick()
    rnd = next(r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take")
    p.studio.refine(rnd["id"], 1, {"prompt": "x"}, note="make her smile more")
    assert p.fv.reshot == [("sh01", "make her smile more", 2)]
    assert not p.store.by_state("draft")  # 派生的草稿作废，不进 Worker
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items["sh01"]["state"] == "running"
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1  # 新的两条 take 再导入一轮
    rnd2 = [r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take"][-1]
    assert [t["params"]["take"] for t in p.store.round_tasks(rnd2["id"])] == [3, 4]
    p.studio.reject(rnd2["id"])  # 不带意见的打回：同样 reshoot
    assert p.fv.reshot[-1] == ("sh01", None, 2)


def test_pause_now_cancels_unfinished_takes(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.pause(pr["id"], stop_running=True)
    assert p.fv.cancelled == "d000000000001"
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 0  # 暂停中：不导入、不推进


def test_film_without_freevideo_refuses(make_orch, fake, tmp_path):
    p = Production(Studio(make_orch(fake)), SkillLibrary((ROOT / "skills", "builtin")),
                   load_workflows(ROOT / "workflows"), FakeLLM(), log=lambda m: None,
                   work_dir=tmp_path / "p")
    assert "freevideo_cards" not in p.tools
    pr = p.create("story-to-film", "x", {"source": "..."})
    assert stage(p.plan(pr["id"]), "clips")["state"] == "blocked"
    with pytest.raises(Exception, match="上游"):
        p.run_stage(pr["id"], "clips")


def test_short_continued_shots_are_lengthened_and_hints_applied(film_prod):
    from video_agent.freevideo import FreeVideoError

    p = film_prod
    board = json.loads(json.dumps(BOARD))
    board["shots"][1]["seconds"] = 6  # 续拍写短了（现在的 schema 会拦）
    calls = []
    real = p.fv.plan_film

    def picky(sp, takes=1):
        calls.append([s["seconds"] for s in sp["shots"]])
        if len(calls) == 1 and sp["shots"][0]["seconds"] != 6:
            raise FreeVideoError(422, {"error": {"code": "invalid_request", "message":
                "x", "next": "Use /screenplay/shots/0/seconds 6"}})
        return real(sp, takes)

    pr = to_film(p)
    art = p._current(p._index(pr["id"]), "storyboard")
    p.store.update_artifact(art["id"], data=board)  # 规则上线前就通过的旧分镜
    p.fv.plan_film = picky
    p.run_stage(pr["id"], "clips")
    sp, _ = p.fv.started[0]
    assert sp["shots"][1]["seconds"] == 10  # 续拍补到 10 秒
    assert sp["shots"][0]["seconds"] == 6 and calls[0][0] == 5  # 按 FreeVideo 的建议改了一次


def test_start_waits_for_freevideo_approval_then_retries_same_ref(film_prod):
    from video_agent.freevideo import FreeVideoError

    p = film_prod
    real, refs, notes = p.fv.start_film, [], []
    p.film.notify_text = lambda prod, key: notes.append(key)

    def guarded(sp, takes=1, ref=None):
        refs.append(ref)
        if len(refs) < 3:  # 你还没在 FreeVideo 页面批准
            raise FreeVideoError(409, {"error": {"code": "needs_confirmation", "message": "approve"}})
        return real(sp, takes, ref)

    p.fv.start_film = guarded
    pr = to_film(p)
    made = p.run_stage(pr["id"], "clips")  # 不报错：挂起等你批准
    assert all(m["meta"]["pending_ref"] and not m["meta"]["film"] for m in made)
    assert notes == ["film_needs_approval"]
    assert stage(p.plan(pr["id"]), "clips")["state"] == "active"
    p.film._last_retry.clear()
    p.film.tick()  # 还没批准
    p.film.tick()  # 60 秒内不重发
    assert len(refs) == 2
    p.film._last_retry.clear()
    p.film.tick()  # 批准了：原样重发（同一个 ref）
    assert len(set(refs)) == 1 and len(refs) == 3
    arts = p.store.artifacts(pr["id"], "clips")
    assert all(a["meta"]["film"] == "d000000000001" and not a["meta"]["pending_ref"] for a in arts)
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1


def test_restart_keeps_film_shots_running(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    assert p.recover() == 0  # 电影镜头不是 LLM 作业：不重排
    assert all(a["status"] == "running" for a in p.store.artifacts(pr["id"], "clips"))
    assert p.pending_skill_jobs() == []


def test_board_style_and_looks_reach_freevideo_and_new_board_starts_new_film(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    assert len(p.fv.started) == 1
    board = {**BOARD, "visual_style": "35mm romance", "continuity": "same outfit",
             "directing_rules": ["no beards"], "negative_prompt": "polyester",
             "characters": {"mei": "Mei, 19, emerald knit top"}}
    board["shots"] = [dict(BOARD["shots"][0], transition_to_next="cut to close-up"), BOARD["shots"][1]]
    p.review(pr["id"], "storyboard", "approve", data=board)  # 手改分镜
    for a in p.store.artifacts(pr["id"], "clips"):  # 旧片的镜头被打回 / 失败后，你点重跑
        p.store.update_artifact(a["id"], status="failed")
    p.run_stage(pr["id"], "clips")
    assert p.fv.reshot == [] and p.fv.cancelled == "d000000000001"  # 剧本变了：不 reshoot 旧片
    sp, _ = p.fv.started[-1]
    assert len(p.fv.started) == 2 and [s["id"] for s in sp["shots"]] == ["sh01", "sh02"]
    assert sp["style"].startswith("35mm romance") and "same outfit" in sp["style"]
    assert "no beards" in sp["style"] and "Avoid: polyester" in sp["style"]
    assert sp["characters"]["mei"]["look"] == "Mei, 19, emerald knit top"
    assert "cut to close-up" in sp["shots"][0]["action"]


def test_cancelled_film_fails_its_unfinished_shots(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.fv.finish_takes("sh01")
    p.fv.film_state["status"] = "cancelled"
    assert p.film.tick() == 1  # 拍完的 take 照样导入
    items = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items == {"sh01": "review", "sh02": "failed"}


def test_rerun_one_shot_after_cancel_starts_film_with_only_that_shot(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.fv.film_state["status"] = "cancelled"
    p.film.tick()
    p.run_stage(pr["id"], "clips", ["sh01"])
    sp, _ = p.fv.started[-1]
    assert p.fv.reshot == [] and [s["id"] for s in sp["shots"]] == ["sh01"]
