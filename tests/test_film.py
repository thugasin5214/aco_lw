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
            # real structure: job is the idempotency key, task is the task ID
            jid, ref = f"j{shot}{i:02d}", f"d1:{shot}:t{i}"
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
    select_all(p)  # casting → character cards (tool, automatic) → storyboard
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    return pr


def test_cards_reuse_library_and_create_from_sheets(film_prod):
    p = film_prod
    pr = to_film(p)
    cards = p._current(p._index(pr["id"]), "cards")["data"]
    # the cat reuses the library card by name
    assert cards["characters"] == {"mei": "c_mei", "cat": "c_cat"}
    (name, desc, images), = p.fv.created
    assert name == "Mei" and desc == "girl, red coat" and images[0][1] == "sheet"
    plan = p.plan(pr["id"])
    assert stage(plan, "clips")["state"] == "ready" and stage(plan, "clips")["confirm"]


def test_film_screenplay_takes_review_and_choose(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")  # you pressed start
    (sp, takes), = p.fv.started
    assert takes == 2 and sp["quality"] == "medium" and sp["characters"]["mei"] == {"card": "c_mei", "language": "English"}
    sh1, sh2 = sp["shots"]
    assert sh1["cast"] == ["mei", "cat"] and sh1["lines"][0]["who"] == "mei" and "continue" not in sh1
    assert sh2["continue"] is True and sh2["seconds"] == 10
    assert p.film.tick() == 0  # no take yet
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1
    items = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items == {"sh01": "review", "sh02": "running"}
    rnd = next(r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take")
    tasks = p.store.round_tasks(rnd["id"])
    assert [t["params"]["take"] for t in tasks] == [1, 2] and all(t["outputs"] for t in tasks)
    assert not any(p.holds(t) is False for t in tasks if t["state"] == "draft")
    p.studio.select(rnd["id"], 2)
    assert p.fv.chosen == [("sh01", 2)]  # the next shot continues from this take
    assert p.film.tick() == 0  # takes already imported are not imported again


def test_redo_and_reject_become_reshoots(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.fv.finish_takes("sh01")
    p.film.tick()
    rnd = next(r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take")
    p.studio.refine(rnd["id"], 1, {"prompt": "x"}, note="make her smile more")
    assert p.fv.reshot == [("sh01", "make her smile more", 2)]
    assert not p.store.by_state("draft")  # the derived draft is dropped, never reaches the Worker
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items["sh01"]["state"] == "running"
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1  # the two new takes are imported as another round
    rnd2 = [r for r in p.store.rounds(None, "review") if r["template"] == "freevideo-take"][-1]
    assert [t["params"]["take"] for t in p.store.round_tasks(rnd2["id"])] == [3, 4]
    p.studio.reject(rnd2["id"])  # reject without a note: also a reshoot
    assert p.fv.reshot[-1] == ("sh01", None, 2)


def test_pause_now_cancels_unfinished_takes(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.pause(pr["id"], stop_running=True)
    assert p.fv.cancelled == "d000000000001"
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 0  # paused: no import, no progress


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
    # continuation written too short (the current schema would block it)
    board["shots"][1]["seconds"] = 6
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
    # an old storyboard approved before the rule existed
    p.store.update_artifact(art["id"], data=board)
    p.fv.plan_film = picky
    p.run_stage(pr["id"], "clips")
    sp, _ = p.fv.started[0]
    assert sp["shots"][1]["seconds"] == 10  # continuation padded to 10 seconds
    # changed once per FreeVideo's suggestion
    assert sp["shots"][0]["seconds"] == 6 and calls[0][0] == 5


def test_start_waits_for_freevideo_approval_then_retries_same_ref(film_prod):
    from video_agent.freevideo import FreeVideoError

    p = film_prod
    real, refs, notes = p.fv.start_film, [], []
    p.film.notify_text = lambda prod, key: notes.append(key)

    def guarded(sp, takes=1, ref=None):
        refs.append(ref)
        if len(refs) < 3:  # you have not approved in the FreeVideo page yet
            raise FreeVideoError(409, {"error": {"code": "needs_confirmation", "message": "approve"}})
        return real(sp, takes, ref)

    p.fv.start_film = guarded
    pr = to_film(p)
    made = p.run_stage(pr["id"], "clips")  # no error: parked waiting for your approval
    assert all(m["meta"]["pending_ref"] and not m["meta"]["film"] for m in made)
    assert notes == ["film_needs_approval"]
    assert stage(p.plan(pr["id"]), "clips")["state"] == "active"
    p.film._last_retry.clear()
    p.film.tick()  # not approved yet
    p.film.tick()  # no resend within 60 seconds
    assert len(refs) == 2
    p.film._last_retry.clear()
    p.film.tick()  # approved: resend unchanged (same ref)
    assert len(set(refs)) == 1 and len(refs) == 3
    arts = p.store.artifacts(pr["id"], "clips")
    assert all(a["meta"]["film"] == "d000000000001" and not a["meta"]["pending_ref"] for a in arts)
    p.fv.finish_takes("sh01")
    assert p.film.tick() == 1


def test_restart_keeps_film_shots_running(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    assert p.recover() == 0  # film shots are not LLM jobs: not re-queued
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
    p.review(pr["id"], "storyboard", "approve", data=board)  # storyboard edited by hand
    # after the old film's shots were rejected / failed, you press rerun
    for a in p.store.artifacts(pr["id"], "clips"):
        p.store.update_artifact(a["id"], status="failed")
    p.run_stage(pr["id"], "clips")
    # the script changed: do not reshoot the old film (it would reuse the old script), start a new
    # one
    assert p.fv.reshot == []
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
    assert p.film.tick() == 1  # finished takes are imported as usual
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


def test_overrides_shoot_a_reviewed_shot_again_in_its_own_film(film_prod):
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.fv.finish_takes("sh01")
    p.film.tick()
    p.run_stage(pr["id"], "clips", ["sh01"], overrides={"quality": "high", "takes": 1})
    sp, takes = p.fv.started[-1]
    assert p.fv.reshot == [] and takes == 1 and sp["quality"] == "high"
    assert [s["id"] for s in sp["shots"]] == ["sh01"]
    # production settings unchanged
    assert p._prod(pr["id"])["settings"].get("film_quality", "medium") == "medium"
    # the old round is still there
    assert any(r["template"] == "freevideo-take" for r in p.store.rounds(None, "review"))


class FakeRM:
    """Records film's calls to the resource manager (the real gating logic is in test_resources)."""

    def __init__(self):
        self.calls, self.films = [], set()

    def prepare_film(self):
        self.calls.append("prepare")
        return []

    def film_started(self, film_id):
        self.calls.append(("started", film_id))
        self.films.add(film_id)

    def films_shooting(self, ids):
        self.calls.append(("shooting", frozenset(ids)))
        self.films = set(ids)


def test_film_start_and_reshoot_make_room_and_report_shooting(film_prod):
    """2026-11-05: shooting bypassed prepare_render, the resident LLM + FreeVideo blew the memory. Memory must be freed before shooting / reshooting."""
    p = film_prod
    rm = p.film.resources = FakeRM()
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    # free memory first, then shoot
    assert rm.calls[:2] == ["prepare", ("started", "d000000000001")]
    p.film.tick()
    assert rm.films == {"d000000000001"}  # some shots have no take yet: shooting
    p.fv.finish_takes("sh01")
    p.fv.finish_takes("sh02")
    p.film.tick()
    assert rm.films == set()  # all done: no longer holds memory
    rm.calls.clear()
    p.film._reshoot(p._prod(pr["id"]), p._wf(p._prod(pr["id"])).stage("clips"), "d000000000001", "sh01", "again", [])
    assert rm.calls == ["prepare", ("started", "d000000000001")]


def test_paused_production_film_still_counts_as_shooting(film_prod):
    p = film_prod
    rm = p.film.resources = FakeRM()
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    p.pause(pr["id"])
    p.film.tick()
    # paused means no progress, but FreeVideo is still shooting
    assert rm.films == {"d000000000001"}
    p.fv.film_state["status"] = "cancelled"
    p.film.tick()
    assert rm.films == set()


def test_film_active():
    from video_agent.film import film_active

    assert film_active({"status": "shooting", "shots": [{"id": "a", "takes": []}]})
    assert film_active({"shots": [{"id": "a", "takes": [{"state": "running"}]}]})
    assert not film_active({"shots": [{"id": "a", "takes": [{"state": "done"}]}]})
    assert not film_active({"status": "cancelled", "shots": [{"id": "a", "takes": []}]})
    idle = {"status": "needs_choice", "shots": [{"id": "a", "takes": [{"state": "failed"}]}, {"id": "b", "takes": []}]}
    assert not film_active(idle)  # waiting for your pick / after an interruption: holds no memory


def test_take_failed_before_a_task_is_imported_without_job_lookup(film_prod):
    """FreeVideo on Mac does not support continuations: the take fails straight away with no task; querying by job key gives not_found and failed the whole poll."""
    p = film_prod
    pr = to_film(p)
    p.run_stage(pr["id"], "clips")
    sh = p.fv.film_state["shots"][0]
    sh["takes"].append({"take": 1, "job": "d1:sh01:t1", "task": None, "state": "failed",
                        "error": {"code": "continuation_unavailable", "error_zh": "当前生成方式不支持接续视频"}})
    assert p.film.tick() == 1  # does not query /jobs (FakeFV.call asserts on unknown ids)
    items = {i["key"]: i["state"] for i in stage(p.plan(pr["id"]), "clips")["items"]}
    assert items["sh01"] == "failed"  # every take failed: the item fails and can be rerun
    (task,) = [t for t in p.store.by_state("failed") if t["template"] == "freevideo-take"]
    notes = [r[0] for r in p.store.db.execute("SELECT note FROM events WHERE task_id=?", (task["id"],))]
    assert task["state"] == "failed" and any("接续" in (n or "") for n in notes)
