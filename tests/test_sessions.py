import json

import pytest
from test_studio import drain, fake_chat, run_jobs

from video_agent.daemon import Actions
from video_agent.i18n import tr
from video_agent.planner import Planner
from video_agent.studio import Studio, StudioError


@pytest.fixture
def studio(make_orch, fake):
    s = Studio(make_orch(fake), round_size=2)
    s.store.set_kv("planner_style", "simple")
    return s


def plan_reply(**kw):
    base = {
        "action": "reply",
        "template": "",
        "prompt": "",
        "image": "",
        "title": "",
        "round_ref": "",
        "pick": 0,
        "role": "",
        "memories": [],
        "reply": "好",
    }
    return {**base, **kw}


def actions_with(studio, responses, **cfg):
    for k, v in cfg.items():
        setattr(studio.orch.cfg, k, v)
    planner = Planner(
        studio.orch.cfg, studio.store, studio.orch.registry, chat=fake_chat(responses)
    )
    out = []
    return Actions(studio, planner), planner, out, (lambda t, b=None: out.append((t, b)))


def test_sessions_share_project_memory_not_history(studio):
    a, planner, _out, reply = actions_with(studio, [plan_reply(), plan_reply(), plan_reply()])
    a.inbound("web", "第一句", reply)
    run_jobs(a)
    studio.add_memory("角色约 19 岁，精致 CG 偏真人")
    s2 = studio.new_session("第二个会话")
    a.inbound("web", "按之前定的风格出一版", reply)
    run_jobs(a)
    msgs = planner.backend.calls[-1]
    assert "角色约 19 岁" in msgs[0]["content"]  # 项目记忆进 system
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "第一句" not in joined  # 别的会话的原话不带过来
    assert studio.current_session()["id"] == s2["id"]
    first = studio.use_session("2")  # 按序号切回
    assert first["id"] != s2["id"]


def test_each_call_extends_the_previous(studio):
    """缓存前提（实测该模型只能“延长”复用）：第 N+1 次请求以第 N 次请求的全部消息原样开头。"""
    a, planner, _out, reply = actions_with(
        studio, [plan_reply(reply="一"), plan_reply(reply="二"), plan_reply(reply="三")]
    )
    rnd = studio.start_round("flux2-t2i", {"seed": 1})  # 让实时状态在两次调用之间变化
    for text in ("甲", "乙", "丙"):
        a.inbound("web", text, reply)
        run_jobs(a)
        drain(studio)
    c1, c2, c3 = planner.backend.calls
    assert c2[: len(c1)] == c1 and c3[: len(c2)] == c2
    assert [m["role"] for m in c3] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert "## 当前状态" in c3[-1]["content"] and "## 当前状态" not in c3[0]["content"]
    assert rnd["id"] in c3[-1]["content"]
    # 预热请求 = 最后一次请求原样
    planner.prewarm(studio.current_session()["id"])
    assert planner.backend.calls[-1] == c3


def test_summarize_rewrites_prefix_once(studio):
    studio.orch.cfg.session_summary_tokens = 1
    studio.orch.cfg.session_keep_messages = 4

    def chat(messages, schema):
        if "summary" in schema["properties"]:
            return json.dumps({"summary": "用户要金发、35mm"}, ensure_ascii=False)
        return json.dumps(plan_reply(), ensure_ascii=False)

    planner = Planner(studio.orch.cfg, studio.store, studio.orch.registry, chat=chat)
    a = Actions(studio, planner)
    reply = lambda t, b=None: None
    for i in range(5):
        a.inbound("web", f"消息{i}", reply)
        run_jobs(a)
    s = studio.current_session()
    assert s["summary"] == "用户要金发、35mm" and s["summary_upto"] > 0
    a.inbound("web", "再来", reply)
    run_jobs(a)
    sys_msg = planner.backend.calls[-1][0]["content"]
    assert "用户要金发、35mm" in sys_msg
    assert "消息0" not in json.dumps(planner.backend.calls[-1], ensure_ascii=False)


def test_memory_proposals_need_confirmation(studio):
    a, _planner, out, reply = actions_with(studio, [plan_reply(memories=["镜头保持 35mm", ""])])
    a.inbound("web", "以后都用 35mm", reply)
    run_jobs(a)
    pid = studio.current_project()["id"]
    assert studio.store.memories(pid, "active") == []
    prop = studio.store.memories(pid, "proposed")
    assert [m["text"] for m in prop] == ["镜头保持 35mm"]
    btn = next(b for t, b in out if b)[0][0][1]
    a.button("web", btn, reply)
    assert [m["text"] for m in studio.store.memories(pid, "active")] == ["镜头保持 35mm"]
    edited = studio.set_memory(prop[0]["id"], "edit", "镜头 35mm，电影感")
    assert edited["supersedes"] == prop[0]["id"]
    assert [m["text"] for m in studio.store.memories(pid, "active")] == ["镜头 35mm，电影感"]


def test_chat_feedback_closes_round_and_links(studio):
    rnd = studio.start_round("flux2-t2i", {"prompt": "girl", "seed": 1})
    drain(studio)
    a, _planner, _out, reply = actions_with(
        studio,
        [
            plan_reply(
                action="round", template="flux2-t2i", prompt="older girl", round_ref=rnd["id"]
            )
        ],
    )
    a.inbound("web", "年龄太小了", reply)
    run_jobs(a)
    assert studio.store.get_round(rnd["id"])["decision"]["action"] == "reject"
    new = studio.store.rounds(state="generating")[0]
    assert new["parent_round"] == rnd["id"]


def test_chat_select_with_role(studio):
    rnd = studio.start_round("flux2-t2i", {"seed": 1})
    drain(studio)
    a, _planner, _out, reply = actions_with(
        studio, [plan_reply(action="select", round_ref=rnd["id"], pick=2, role="角色")]
    )
    a.inbound("web", "就用 2 号，锁成角色", reply)
    run_jobs(a)
    assert studio.store.get_round(rnd["id"])["decision"]["idx"] == 2
    assert "角色" in studio.store.pack(rnd["project_id"])["assets"]


def test_upload_reference_and_tweak(studio):
    up = studio.save_upload("ref.png", b"\x89PNG fake")
    assert studio.orch.resolve_ref(f"@upload:{up['id']}", None) == up["path"]
    with pytest.raises(StudioError):
        studio.save_upload("x.exe", b"MZ")
    rnd = studio.start_round("flux2-t2i", {"prompt": "a", "seed": 5}, n=1)
    drain(studio)
    t = studio.store.round_tasks(rnd["id"])[0]
    tw = studio.tweak(t["id"], {"prompt": "b", "steps": 6}, n=2)
    tasks = studio.store.round_tasks(tw["id"])
    assert [x["params"]["seed"] for x in tasks] == [5, 6]
    assert all(x["params"]["prompt"] == "b" and x["parent_id"] == t["id"] for x in tasks)


def test_orphan_messages_become_history_session(studio):
    pid = studio.current_project()["id"]
    studio.store.log_message("web", "in", "旧消息", pid)  # 旧版本没有 session_id
    studio.store.set_kv(f"session:{pid}", None)
    for s in studio.store.sessions(pid):
        studio.store.update_session(s["id"], state="closed")
    s = studio.current_session(pid)
    assert s["title"] == tr("history_session")
    assert [m["text"] for m in studio.store.session_messages(s["id"])][-1] == "旧消息"


def test_change_decision_reverts_pack_and_keeps_history(studio):
    first = studio.start_round("flux2-t2i", {"seed": 1}, n=1)
    drain(studio)
    studio.select(first["id"], 1, role="character")  # pack v1: character ← first
    rnd = studio.start_round("flux2-t2i", {"seed": 10})
    drain(studio)
    studio.select(rnd["id"], 2, role="character")  # pack v2: character ← rnd #2
    t2 = studio.store.round_tasks(rnd["id"])[1]["id"]
    assert studio.store.pack(rnd["project_id"])["assets"]["character"]["task_id"] == t2

    reopened, running = studio.reopen(rnd["id"])
    assert reopened["state"] == "review" and running == 0
    assert {t["state"] for t in studio.store.round_tasks(rnd["id"])} == {"review"}
    pack = studio.store.pack(rnd["project_id"])  # v3 restores the earlier asset
    assert pack["version"] == 3
    first_task = studio.store.round_tasks(first["id"])[0]["id"]
    assert pack["assets"]["character"]["task_id"] == first_task

    studio.select(rnd["id"], 1)  # decide again
    d = studio.store.get_round(rnd["id"])["decision"]
    assert d["idx"] == 1 and d["history"][0]["idx"] == 2
    with pytest.raises(StudioError):
        studio.reopen(studio.start_round("flux2-t2i", {"seed": 99}, n=1)["id"])  # not decided


def test_reopen_reports_spawned_rounds_and_standalone_task(studio):
    rnd = studio.start_round("flux2-t2i", {"seed": 3}, n=1)
    drain(studio)
    child = studio.redo(rnd["id"])
    _, running = studio.reopen(rnd["id"])
    assert running == 1 and studio.store.get_round(child["id"])["state"] == "generating"
    t = studio.orch.create("flux2-t2i", {"seed": 55})
    studio.orch.execute(t["id"])
    studio.approve_task(t["id"], False)
    assert studio.reopen_task(t["id"])["state"] == "review"


def test_unmeasured_video_tier_falls_back_with_opt_in_button(studio):
    img = studio.start_round("flux2-t2i", {"seed": 1}, n=1)
    drain(studio)
    a, _p, out, reply = actions_with(
        studio,
        [
            plan_reply(
                action="round",
                template="h3-i2v",
                prompt="she smiles",
                tier="hd",
                image=f"@task:{studio.store.round_tasks(img['id'])[0]['id']}",
            )
        ],
    )
    a.inbound("web", "make a video", reply)
    run_jobs(a)
    rnd = studio.store.rounds(state="generating")[0]
    assert rnd["tier"] == "smoke"  # 自动降到已实测档位
    btn = next(b for t, b in out if b)[0][0][1]
    assert btn == f"u:{rnd['id']}:hd"
    a.button("web", btn, reply)  # 用户确认后才跑 hd
    tiers = sorted(r["tier"] for r in studio.store.rounds(state="generating"))
    assert tiers == ["hd", "smoke"]


def test_explicit_tier_in_user_text_is_honoured(studio):
    from video_agent.daemon import explicit_tier

    assert explicit_tier("做成hd，1分钟", "hd") and explicit_tier("高清版", "hd")
    assert explicit_tier("hd 60s please", "hd-60s") and not explicit_tier("hd please", "hd-60s")
    assert not explicit_tier("make a video", "hd")
    img = studio.start_round("flux2-t2i", {"seed": 1}, n=1)
    drain(studio)
    a, _p, out, reply = actions_with(
        studio,
        [
            plan_reply(
                action="round",
                template="h3-i2v",
                prompt="p",
                tier="hd",
                image=f"@task:{studio.store.round_tasks(img['id'])[0]['id']}",
            )
        ],
    )
    a.inbound("web", "做成hd", reply)
    run_jobs(a)
    assert studio.store.rounds(state="generating")[0]["tier"] == "hd"  # 不降档
    assert not any(b for _t, b in out)  # 不再弹“也试一下”
