import json

import pytest
from test_studio import drain

from video_agent.planner import Planner
from video_agent.studio import Studio, StudioError
from video_agent.variants import Workshop, diff_graph


@pytest.fixture
def ws(make_orch, fake):
    return Workshop(Studio(make_orch(fake), round_size=2))


def exported(ws, **kw):
    """Run a real task first and take the workflow it submitted (as if exported from the console)."""
    rnd = ws.studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 7}, n=1, **kw)
    drain(ws.studio)
    task = ws.store.round_tasks(rnd["id"])[0]
    _, data = ws.export(task["id"])
    return task, json.loads(data)


def test_import_diff_trial_promote(ws):
    task, g = exported(ws)
    g["12"]["inputs"]["steps"] = 6  # steps changed in ComfyUI
    g["11"]["inputs"]["sampler_name"] = "euler"
    g["7"]["inputs"]["text"] = "girl, film grain"
    meta = ws.import_graph(json.dumps(g).encode(), "flux2-t2i", task_id=task["id"])
    assert meta["status"] == "candidate" and not meta["problems"]
    changed = {(c["id"], c["field"]) for c in meta["diff"]["changed"]}
    assert ("12", "steps") in changed and ("7", "text") in changed
    assert meta["name"] in ws.orch.registry.templates
    # the planner does not see candidates
    assert meta["name"] not in ws.orch.registry.builtin_and_user()

    meta = ws.trial(meta["id"])
    trial_task = ws.store.round_tasks(meta["trial"]["round_id"])[0]
    # uses the value edited in the graph, not the original task's
    assert trial_task["params"]["steps"] == 6
    assert trial_task["params"]["prompt"] == "girl, film grain"
    assert trial_task["tier"] == "preview"  # lowest tier
    with pytest.raises(StudioError, match="successful trial"):
        ws.promote(meta["id"], "flux2-film")
    drain(ws.studio)
    meta = ws.refresh(meta["id"])
    assert meta["status"] == "trial_ok" and meta["trial"]["result"]["ok"]

    with pytest.raises(StudioError, match="lowercase"):
        ws.promote(meta["id"], "Bad Name")
    meta = ws.promote(meta["id"], "flux2-film")
    t = ws.orch.registry.template("flux2-film")
    assert t.origin == "user" and t.tiers["preview"].verified and not t.tiers["standard"].verified
    assert "flux2-film" in ws.orch.registry.builtin_and_user()
    assert meta["name"] not in ws.orch.registry.templates  # the variant itself leaves the registry
    planner = Planner(ws.orch.cfg, ws.store, ws.orch.registry, chat=lambda m, s: "{}")
    assert "flux2-film" in planner._templates_text()


def test_removed_node_drops_slot_and_bad_graph_is_invalid(ws):
    task, g = exported(ws)
    del g["10"]  # remove RandomNoise → the seed slot is gone and a link points to a missing node
    meta = ws.import_graph(json.dumps(g).encode(), "flux2-t2i", task_id=task["id"])
    assert "seed" in meta["dropped_slots"]
    assert meta["status"] == "invalid" and any("10" in p for p in meta["problems"])  # dangling link
    g2 = dict(g)
    g2["99"] = {"class_type": "NoSuchNode", "inputs": {}}
    bad = ws.import_graph(json.dumps(g2).encode(), "flux2-t2i", task_id=task["id"])
    assert bad["status"] == "invalid" and any("NoSuchNode" in p for p in bad["problems"])
    assert bad["name"] not in ws.orch.registry.templates
    with pytest.raises(StudioError, match="Export \\(API\\)"):
        ws.import_graph(b'{"nodes": [], "links": []}', "flux2-t2i")  # canvas format
    with pytest.raises(StudioError, match="valid JSON"):
        ws.import_graph(b"not json", "flux2-t2i")


def test_reject_cancels_queued_variant_tasks(ws):
    task, g = exported(ws)
    meta = ws.import_graph(json.dumps(g).encode(), "flux2-t2i", task_id=task["id"])
    ws.trial(meta["id"])
    ws.reject(meta["id"])
    trial_task = ws.store.round_tasks(ws.get(meta["id"])["trial"]["round_id"])[0]
    assert trial_task["state"] == "failed"
    assert meta["name"] not in ws.orch.registry.templates
    assert [v["status"] for v in ws.list()] == ["rejected"]


def test_diff_graph_classifies_changes():
    a = {
        "1": {"class_type": "A", "inputs": {"x": 1, "y": ["2", 0]}},
        "2": {"class_type": "B", "inputs": {}},
    }
    b = {
        "1": {"class_type": "A", "inputs": {"x": 2, "y": ["3", 0]}},
        "3": {"class_type": "C", "inputs": {}},
    }
    d = diff_graph(a, b)
    assert d["added"] == [{"id": "3", "class": "C"}] and d["removed"] == [{"id": "2", "class": "B"}]
    assert d["changed"][0]["field"] == "x" and d["rewired"][0]["field"] == "y"
