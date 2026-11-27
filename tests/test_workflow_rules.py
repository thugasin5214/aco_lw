"""Workflow mode: each workflow carries its rules (YAML → data/workflow_rules.yaml → the
production's settings, per stage too); stopping or rejecting an item stops everything after it."""

import json

from video_agent.production import Production

from .test_production import prod, run_skills, stage  # noqa: F401 — prod is a fixture
from .test_studio import drain


def to_keyframes(p: Production):
    """novel-to-film up to keyframes rendering (casting picked, storyboard approved)."""
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    casting = drain(p.studio)
    for rnd in casting:
        p.studio.select(rnd["id"], 1)
    run_skills(p)
    p.review(pr["id"], "storyboard", "approve")
    assert stage(p.plan(pr["id"]), "keyframes")["state"] == "active"
    return pr


def rerun_mei(p, pid):
    """Re-render Mei's casting by hand (the approved one stays until a new one is picked)."""
    art = p.run_stage(pid, "characters", ["mei"], "again", "web")[0]
    return p.store.get_round(art["round_id"])


def keyframe_rounds(p, pid):
    return [
        r
        for r in p.store.production_rounds(pid)
        if r["request"].startswith(f"production:{pid}:keyframes:")
    ]


def test_downstream_follows_needs():
    from video_agent.config import ROOT
    from video_agent.production import load_workflows

    wf = load_workflows(ROOT / "workflows")["novel-to-film"]
    assert Production.downstream(wf, "characters") == [
        "storyboard",
        "keyframes",
        "clips",
        "edit",
        "final",
    ]
    assert Production.downstream(wf, "final") == []


def test_stopping_an_item_stops_everything_after_it(prod):  # noqa: F811
    p = prod
    pr = to_keyframes(p)
    kfs = keyframe_rounds(p, pr["id"])
    assert kfs and all(r["state"] == "generating" for r in kfs)
    # you redo Mei's casting and stop it halfway (⏹): the keyframes made from her stop too
    new = rerun_mei(p, pr["id"])
    p.studio.stop_round(new["id"])
    for r in keyframe_rounds(p, pr["id"]):
        assert r["state"] != "generating"
        assert all(t["state"] == "failed" for t in p.store.round_tasks(r["id"]))
        assert p.store.artifact_by_round(r["id"])["meta"]["stopped"]
    # the stopped casting item waits for you: no auto-pick
    assert p.store.artifact_by_round(new["id"])["meta"]["stopped"]
    log = [m["text"] for m in p.store.messages(pr["project_id"])]
    assert any("keyframes" in t or "characters" in t for t in log)


def test_rejecting_a_planning_stage_stops_the_renders_after_it(prod):  # noqa: F811
    p = prod
    pr = to_keyframes(p)
    p.review(pr["id"], "storyboard", "reject", note="wrong order")
    assert all(r["state"] != "generating" for r in keyframe_rounds(p, pr["id"]))


def test_rules_from_user_file_and_production_settings(prod, tmp_path):  # noqa: F811
    p = prod
    rules = tmp_path / "workflow_rules.yaml"
    rules.write_text(
        "novel-to-film:\n  guidance: draft first\n  stages:\n    characters: {on_reject: wait}\n"
    )
    p.rules_path = rules
    pr = p.create(
        "novel-to-film", "Lost cat", {"source": "..."}, settings={"rules": {"cascade": False}}
    )
    prod_row = p.store.get_production(pr["id"])
    assert p.rules(prod_row) == {
        "cascade": False,
        "on_reject": "rerender",
        "guidance": "draft first",
    }
    assert p.rules(prod_row, "characters")["on_reject"] == "wait"
    assert p.plan(pr["id"])["rules"]["guidance"] == "draft first"
    run_skills(p)
    p.review(pr["id"], "adaptation", "approve")
    run_skills(p)
    p.review(pr["id"], "bible", "approve")
    mei = next(r for r in drain(p.studio) if r["title"].endswith("mei"))
    p.studio.reject(mei["id"])  # no note, but the rule says wait: no automatic re-render
    items = {i["key"]: i for i in stage(p.plan(pr["id"]), "characters")["items"]}
    assert items["mei"]["state"] == "rejected"


def test_no_cascade_when_the_rules_turn_it_off(prod):  # noqa: F811
    p = prod
    pr = to_keyframes(p)
    p.update_settings(pr["id"], {"rules": {"cascade": False}})
    p.studio.stop_round(rerun_mei(p, pr["id"])["id"])
    assert all(r["state"] == "generating" for r in keyframe_rounds(p, pr["id"]))


def test_skill_result_of_a_stopped_item_is_dropped(prod):  # noqa: F811
    p = prod
    pr = p.create("novel-to-film", "Lost cat", {"source": "..."})
    run_skills(p)  # brief → adaptation waits for review
    p.review(pr["id"], "adaptation", "approve")
    job = p.pending_skill_jobs()[0]
    assert job["stage"] == "bible"
    real = p.llm

    def stopped_meanwhile(stage_, messages, schema):
        out = real(stage_, messages, schema)
        p.stop_downstream(pr["id"], "adaptation", "adaptation was stopped")
        return out

    p.llm = stopped_meanwhile
    p.run_skill_job(job)
    art = p.store.get_artifact(job["id"])
    assert art["status"] == "rejected" and art["note"].startswith("stopped")
    assert json.dumps(art["data"] or {}) == "{}" or not art["data"]
