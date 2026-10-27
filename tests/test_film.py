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
            jid = f"j{shot}{i:02d}"
            sh["takes"].append({"take": i, "job": jid})
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
