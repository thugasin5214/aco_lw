import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from video_agent.daemon import Actions, Worker
from video_agent.i18n import tr
from video_agent.planner import Planner
from video_agent.render import ParamError
from video_agent.studio import Studio, StudioError
from video_agent.telegram import TelegramAPI, TelegramBot


@pytest.fixture
def studio(make_orch, fake):
    s = Studio(make_orch(fake), round_size=3)
    # these tests cover single-shot JSON planning (simple mode)
    s.store.set_kv("planner_style", "simple")
    return s


def drain(studio: Studio) -> list[dict]:
    """Run all queued tasks synchronously (instead of the Worker thread). Returns rounds that just entered review."""
    ready = []
    while (t := studio.store.next_draft()) is not None:
        studio.orch.execute(t["id"])
        if t["round_id"]:
            rnd, became = studio.refresh_round(t["round_id"])
            if became:
                ready.append(rnd)
    return ready


def fake_chat(responses: list[dict]):
    calls = []

    def chat(messages, schema):
        calls.append(messages[-1]["content"])
        return json.dumps(responses.pop(0), ensure_ascii=False)

    chat.calls = calls
    return chat


# ---- projects / rounds / decisions ----
def test_round_lifecycle_and_select_lock(studio):
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 100})
    tasks = studio.store.round_tasks(rnd["id"])
    assert [t["params"]["seed"] for t in tasks] == [100, 101, 102]
    assert rnd["state"] == "generating"
    assert drain(studio)[0]["id"] == rnd["id"]
    with pytest.raises(StudioError):
        studio.select(rnd["id"], 9)
    studio.select(rnd["id"], 2, role="角色")
    states = [t["state"] for t in studio.store.round_tasks(rnd["id"])]
    assert states == ["rejected", "done", "rejected"]
    pack = studio.store.pack(rnd["project_id"])
    assert pack["version"] == 1 and pack["assets"]["角色"]["seed"] == 101
    with pytest.raises(StudioError, match="已经做过决定"):
        studio.select(rnd["id"], 1)


def test_pack_reference_resolves(studio):
    rnd = studio.start_round("flux2-t2i", {"seed": 1}, n=1)
    drain(studio)
    studio.select(rnd["id"], 1, role="角色")
    vid = studio.start_round("h3-i2v", {"image": "@pack:角色"})
    t = studio.store.round_tasks(vid["id"])[0]
    assert t["params"]["image"].endswith("v1/角色.png")
    assert vid["n"] == 1  # video defaults to 1 per round


def test_redo_refine_reject(studio):
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 10})
    drain(studio)
    redo = studio.redo(rnd["id"])
    assert redo["parent_round"] == rnd["id"] and redo["n"] == 3
    drain(studio)
    ref = studio.refine(redo["id"], 2, {"prompt": "b"}, "更亮")
    seeds = [t["params"]["seed"] for t in studio.store.round_tasks(ref["id"])]
    base_seed = studio.store.round_tasks(redo["id"])[1]["params"]["seed"]
    assert seeds[0] == base_seed
    assert all(t["params"]["prompt"] == "b" for t in studio.store.round_tasks(ref["id"]))
    with pytest.raises(StudioError, match="还在生成"):
        studio.reject(ref["id"], "x")
    drain(studio)
    assert studio.reject(ref["id"], "不要")["decision"]["action"] == "reject"


def test_finish_project_keeps_history(studio):
    p1 = studio.current_project()
    studio.start_round("flux2-t2i", {"seed": 5})
    studio.finish_project(note="收工")
    assert studio.store.get_project(p1["id"])["state"] == "done"
    assert all(t["state"] == "failed" for t in studio.store.list(project_id=p1["id"]))
    assert studio.store.messages(p1["id"])  # chat is kept
    p2 = studio.current_project()
    assert p2["id"] != p1["id"]
    assert {p["id"] for p in studio.store.list_projects()} >= {p1["id"], p2["id"]}


def test_approve_standalone_task(studio):
    t = studio.orch.create("flux2-t2i", {"seed": 77})
    studio.orch.execute(t["id"])
    studio.approve_task(t["id"], True)
    assert studio.store.get(t["id"])["state"] == "done"
    r = studio.redo_task(t["id"])
    assert r["n"] == 3


# ---- planner ----
def test_planner_validates_output(studio):
    store, reg = studio.store, studio.orch.registry
    ok = Planner(
        studio.orch.cfg,
        store,
        reg,
        chat=fake_chat(
            [
                {
                    "action": "round",
                    "template": "flux2-t2i",
                    "prompt": "x",
                    "image": "",
                    "reply": "好",
                }
            ]
        ),
    )
    plan = ok.plan_request("出一张图", studio.current_project()["id"])
    assert plan.template == "flux2-t2i" and plan.params == {"prompt": "x"}
    assert store.messages()[-1]["channel"] == "llm"
    bad = Planner(
        studio.orch.cfg,
        store,
        reg,
        chat=fake_chat([{"action": "round", "template": "nope", "prompt": "x", "reply": ""}]),
    )
    with pytest.raises(ParamError):
        bad.plan_request("超大图", None)
    note = Planner(
        studio.orch.cfg,
        store,
        reg,
        chat=fake_chat(
            [{"prompt": "b", "image": "/etc/passwd", "steps": 99, "memories": [], "reply": ""}]
        ),
    )
    delta, _, _ = note.translate_note("flux2-edit", {"prompt": "a"}, "换图", None)
    # out-of-range steps and the image field are ignored, the prompt change is kept
    assert delta == {"prompt": "b"}


# ---- Actions: commands, buttons, notes ----
def run_jobs(actions: Actions) -> None:
    while not actions.jobs.empty():
        actions.jobs.get()[0]()


def test_actions_buttons_and_notes(studio):
    planner = Planner(
        studio.orch.cfg,
        studio.store,
        studio.orch.registry,
        chat=fake_chat(
            [
                {"prompt": "sad eyes", "reply": "改成忧伤眼神"},
                {
                    "action": "round",
                    "template": "flux2-t2i",
                    "prompt": "new",
                    "image": "",
                    "n": 2,
                    "reply": "出两张",
                },
            ]
        ),
    )
    actions = Actions(studio, planner)
    out = []
    reply = lambda text, buttons=None: out.append((text, buttons))
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 1})
    drain(studio)
    actions.button("telegram", f"f:{rnd['id']}:2", reply)
    actions.inbound("telegram", "眼神再忧伤一点", reply)
    run_jobs(actions)
    new = studio.store.rounds(state="generating")[0]
    assert new["parent_round"] == rnd["id"]
    assert studio.store.round_tasks(new["id"])[0]["params"]["prompt"] == "sad eyes"
    actions.inbound("telegram", "做一个新的场景", reply)
    run_jobs(actions)
    assert any("出两张" in t for t, _ in out)
    actions.inbound("telegram", "/pause", reply)
    assert studio.store.get_kv("paused") is True
    actions.inbound("telegram", "/mode command_only", reply)
    actions.inbound("telegram", "随便说一句", reply)
    assert tr("command_only") in out[-1][0]
    hist = studio.store.messages(studio.current_project()["id"])
    assert {m["direction"] for m in hist} == {"in", "out"}


def test_telegram_routing_and_whitelist(studio):
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req.url.path.rsplit("/", 1)[1], req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    api = TelegramAPI("T", transport=httpx.MockTransport(handler))
    actions = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    bot = TelegramBot(api, actions, allowed=[42])
    msg = lambda chat, text: {"message": {"chat": {"id": chat}, "text": text}}
    bot.handle(msg(99, "/status"))
    assert sent == []  # non-whitelisted chats get no answer
    bot.handle(msg(99, "/whoami"))
    assert b"99" in sent[-1][1]
    bot.handle(msg(42, "/round flux2-t2i 2 a cat"))
    assert studio.store.rounds(state="generating")[0]["n"] == 2
    rnd = studio.store.rounds(state="generating")[0]
    drain(studio)
    bot.send_round(
        studio.store.get_round(rnd["id"]),
        studio.store.round_tasks(rnd["id"]),
        studio.store.task_dir,
    )
    assert "sendMediaGroup" in [m for m, _ in sent]
    bot.handle(
        {
            "callback_query": {
                "id": "c",
                "data": f"s:{rnd['id']}:1",
                "message": {"message_id": 5, "chat": {"id": 42}},
            }
        }
    )
    assert studio.store.get_round(rnd["id"])["decision"]["idx"] == 1
    assert "editMessageReplyMarkup" in [m for m, _ in sent]


# ---- Web ----
def test_web_review_endpoints(studio, tmp_path):
    from video_agent.web import create_app

    actions = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    c = TestClient(create_app(studio.orch.cfg, actions))
    rnd = studio.start_round("flux2-t2i", {"seed": 3})
    drain(studio)
    review = c.get("/api/review").json()
    assert review[0]["id"] == rnd["id"] and len(review[0]["tasks"]) == 3
    g = c.get("/api/graph").json()
    assert g[0]["type"] == "round"
    r = c.post(f"/api/rounds/{rnd['id']}/select", json={"idx": 3, "role": "场景"})
    assert r.status_code == 200
    assert c.post(f"/api/rounds/{rnd['id']}/redo").status_code == 400  # already decided
    p = c.get(f"/api/projects/{rnd['project_id']}").json()
    assert p["packs"][0]["assets"]["场景"]
    name = p["packs"][0]["assets"]["场景"]["file"].split("/")[-1]
    assert c.get(f"/packfile/{rnd['project_id']}/v1/{name}").status_code == 200
    assert c.get("/packfile/x/../../tasks.sqlite/a").status_code == 404
    out = c.post("/api/chat", json={"text": "/project new 第二个项目"}).json()
    assert "第二个项目" in out["messages"][0]["text"]
    assert len(c.get("/api/projects").json()) == 2


# ---- Worker ----
def test_worker_runs_round_and_notifies(studio):
    notified = []
    stop = threading.Event()
    w = Worker(studio.orch, studio, notified.append, stop)
    rnd = studio.start_round("flux2-t2i", {"seed": 9})
    w.start()
    deadline = time.time() + 10
    while not notified and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    w.join(5)
    assert notified and notified[0]["id"] == rnd["id"] and notified[0]["state"] == "review"


def test_worker_survives_comfy_offline_at_startup(studio, monkeypatch):
    """The daemon can start before ComfyUI: a failed startup reconcile is retried,
    the worker thread must not die."""
    calls = []
    real = studio.orch.recover

    def recover():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("Connection refused")
        return real()

    monkeypatch.setattr(studio.orch, "recover", recover)
    notified = []
    stop = threading.Event()
    w = Worker(studio.orch, studio, notified.append, stop)
    waits = []
    real_wait = stop.wait
    monkeypatch.setattr(stop, "wait", lambda t=None: waits.append(t) or real_wait(0.01))
    rnd = studio.start_round("flux2-t2i", {"seed": 9})
    w.start()
    deadline = time.time() + 10
    while not notified and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    w.join(5)
    assert len(calls) == 2 and waits[0] == 30
    assert notified and notified[0]["id"] == rnd["id"]


def test_video_task_reference_uses_mid_frame(studio, monkeypatch):
    from video_agent.scheduler import Orchestrator

    rnd = studio.start_round("h3-t2v", {"seed": 4})
    drain(studio)
    vid = studio.store.round_tasks(rnd["id"])[0]
    studio.store.update(vid["id"], outputs=[{"file": "v.mp4", "kind": "video"}])
    seen = {}

    def fake_frame(video):
        seen["video"] = video
        return video.with_name("v_mid.png")

    monkeypatch.setattr(Orchestrator, "_video_frame", staticmethod(fake_frame))
    path = studio.orch.resolve_ref(f"@task:{vid['id']}", None)
    assert path.endswith("v_mid.png") and seen["video"].name == "v.mp4"
    with pytest.raises(ValueError, match="找不到任务"):
        studio.orch.resolve_ref("@task:nope", None)


def test_telegram_photo_becomes_upload(studio):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/p1.jpg"}})
        if "/file/bot" in path:
            return httpx.Response(200, content=b"\xff\xd8 jpeg")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    api = TelegramAPI("T", transport=httpx.MockTransport(handler))
    seen = []
    actions = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    actions.request = lambda text, ch, say, mid=None, att=None: seen.append((text, att))
    bot = TelegramBot(api, actions, allowed=[42])
    bot.handle(
        {
            "message": {
                "chat": {"id": 42},
                "caption": "参照这张",
                "photo": [{"file_id": "s"}, {"file_id": "big"}],
            }
        }
    )
    text, att = seen[0]
    assert text == "参照这张" and att[0]["name"] == "p1.jpg"
    assert studio.store.get_upload(att[0]["id"])["path"].endswith(".jpg")


def test_note_on_video_template_ignores_steps(studio):
    """The h3 template has no steps (only base_steps / refine_steps): the model giving steps must not fail the whole note."""
    planner = Planner(
        studio.orch.cfg,
        studio.store,
        studio.orch.registry,
        chat=fake_chat(
            [{"prompt": "she smiles and turns", "steps": 20, "memories": [], "reply": "ok"}]
        ),
    )
    delta, _, _ = planner.translate_note("h3-i2v", {"prompt": "she walks"}, "more smile", None)
    assert delta == {"prompt": "she smiles and turns"}


def test_delete_project_removes_everything_of_it(studio):
    p1 = studio.current_project()
    rnd = studio.start_round("flux2-t2i", {"seed": 5})
    drain(studio)
    studio.add_memory("camera stays at 35mm")
    studio.log("web", "in", "hello", p1["id"])
    other = studio.new_project("other")
    keep = studio.start_round("flux2-t2i", {"seed": 6}, project_id=other["id"])
    files = [o for t in studio.store.round_tasks(rnd["id"]) for o in t["outputs"]]
    out = studio.delete_project(p1["id"])
    assert out["tasks"] == 3 and out["sessions"] >= 1
    assert studio.store.get_project(p1["id"]) is None
    assert studio.store.get_round(rnd["id"]) is None
    assert studio.store.messages(p1["id"]) == [] and studio.store.sessions(p1["id"]) == []
    assert studio.store.memories(p1["id"]) == []
    assert all(not (studio.store.jobs_dir / f).exists() for f in files if isinstance(f, str))
    assert studio.store.get_round(keep["id"]) is not None


def test_purge_clears_review_outputs_outside_any_project(studio):
    t = studio.orch.create("flux2-t2i", {"seed": 77})
    studio.orch.execute(t["id"])
    studio.store.db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (t["id"],))
    assert studio.store.get(t["id"])["state"] == "review"
    out = studio.purge_project("none", "review")
    assert out["tasks"] == 1 and studio.store.get(t["id"]) is None


def test_progress_and_architecture_pages_are_local_only(studio):
    """They are not in the public repository; without the local copy the tab says so."""
    from video_agent.config import ROOT
    from video_agent.web import create_app

    actions = Actions(studio, Planner(studio.orch.cfg, studio.store, studio.orch.registry))
    c = TestClient(create_app(studio.orch.cfg, actions))
    assert not (ROOT / "docs" / "progress.html").exists()
    for url in ("/progress", "/architecture", "/architecture.zh-CN.html"):
        r = c.get(url)
        assert r.status_code == 200
        local = (ROOT / "internal" / "docs" / "architecture.html").exists()
        assert local or "developer's machine" in r.text
