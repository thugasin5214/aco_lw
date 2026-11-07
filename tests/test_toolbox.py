"""通用工具层：agent 提出 → 编排器执行（命令先经批准、后台跑）→ 结果按内存情况交回 LLM。"""

import json
import time

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — env 是 fixture

from video_agent.toolbox import blocked


def wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def pending_approval(studio, run_id):
    return next(
        m
        for m in studio.store.agent_msgs(run_id)
        if m["role"] == "tool" and m["status"] != "done" and m["meta"].get("kind") == "approval"
    )


def tool_result(studio, run_id, name):
    m = next(
        m
        for m in studio.store.agent_msgs(run_id)
        if m["role"] == "tool" and m["name"] == name and m["status"] == "done"
    )
    return json.loads(m["content"])


def test_command_needs_approval_runs_in_background_and_resumes(env):  # noqa: F811
    studio, make, said = env
    a, _script = make(
        {
            "tool_calls": [
                call("run_command", {"command": "echo hi > out.txt && echo ok", "reason": "test"})
            ]
        },
        {"text": "done"},
    )
    a.inbound("web", "run it", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert run["status"] == "waiting_user"  # 等你批准，命令还没跑
    assert any("echo hi" in t and b for t, b in said)  # 原样展示命令 + 按钮
    m = pending_approval(studio, run["id"])
    a.runtime.on_approval(m["id"], True)
    assert wait_until(lambda: a.jobs.qsize() > 0)  # 跑完：排一步把结果交回 LLM
    run_all(a)
    assert last_run(studio)["status"] == "done"
    res = tool_result(studio, run["id"], "run_command")
    assert res["exit_code"] == 0 and "ok" in res["stdout"] and "out.txt" in res["new_files"]
    assert (studio.orch.cfg.data_dir / "agent-work" / run["id"] / "out.txt").read_text() == "hi\n"


def test_declined_command_never_runs(env):  # noqa: F811
    studio, make, _ = env
    a, _ = make(
        {"tool_calls": [call("run_command", {"command": "touch nope", "reason": "x"})]},
        {"text": "ok, not running it"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    a.runtime.on_approval(pending_approval(studio, run["id"])["id"], False)
    run_all(a)
    assert last_run(studio)["status"] == "done"
    assert tool_result(studio, run["id"], "run_command")["approved"] is False
    assert not (studio.orch.cfg.data_dir / "agent-work" / run["id"] / "nope").exists()


def test_generation_and_llm_side_effects_are_refused_even_before_approval(env):  # noqa: F811
    studio, make, said = env
    a, _ = make(
        {
            "tool_calls": [
                call("run_command", {"command": "curl http://127.0.0.1:8188/queue", "reason": "x"})
            ]
        },
        {"text": "ok"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert "refused" in tool_result(studio, run["id"], "run_command")["error"]
    assert not any(b for _, b in said)  # 没有发审批按钮
    assert blocked("cat ~/x/FreeVideo-local/agent/token")
    assert blocked("lms unload --all") and blocked("pkill -f ComfyUI")
    assert blocked("ffmpeg -i a.mp4 b.mp4") is None


def test_auto_allow_only_for_single_commands(env):  # noqa: F811
    _studio, make, _ = env
    a, _ = make()
    tb = a.runtime.toolbox
    a.runtime.policies.data["execution"]["tools"]["run_command"]["auto_allow"] = ["ffprobe"]
    assert tb.auto_allowed("ffprobe -v error x.mp4")
    assert not tb.auto_allowed("ffprobe x.mp4; rm -rf ~")
    assert not tb.auto_allowed("ffprobe x.mp4 | sh")
    assert not tb.auto_allowed("ffprobex")


def test_job_result_waits_for_memory_before_reaching_llm(env):  # noqa: F811
    """命令跑完时 LLM 被卸载（在拍片）：结果暂存，内存空出来再交回。"""
    studio, make, said = env
    a, script = make(
        {"tool_calls": [call("run_command", {"command": "echo late", "reason": "x"})]},
        {"text": "got it"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    rm = studio.orch.resources
    rm.film_started("f1")
    a.runtime.on_approval(pending_approval(studio, run["id"])["id"], True)
    assert wait_until(lambda: a.jobs.qsize() > 0)
    run_all(a)
    assert last_run(studio)["status"] == "waiting_resources" and len(script.seen) == 1
    assert "late" in tool_result(studio, run["id"], "run_command")["stdout"]  # 已落库
    rm.films_shooting(set())
    run_all(a)
    assert last_run(studio)["status"] == "done" and said[-1][0] == "got it"


def test_files_skills_memory_and_media(env, tmp_path):  # noqa: F811
    studio, make, _ = env
    cfg = studio.orch.cfg
    sk = tmp_path / "ext" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use for demo links\n---\nRun `demo`."
    )
    (sk / "notes.md").write_text("extra")
    cfg.agent_skill_dirs = [str(tmp_path / "ext")]
    a, _ = make()
    tb = a.runtime.toolbox
    assert "demo: Use for demo links" in a.runtime.system_prompt(
        studio.current_project()["id"], None
    )
    s = tb.load_skill("demo")
    assert "Run `demo`" in s["instructions"] and s["files"] == ["notes.md"]
    assert tb.read_file("r1", str(sk / "notes.md"), 0, 10)["text"] == "extra"
    tb.write_file("r1", "a/b.txt", "x")
    assert tb.read_file("r1", "a/b.txt", 0, 10)["text"] == "x"
    with pytest.raises(ValueError):
        tb.write_file("r1", "../../escape.txt", "x")
    with pytest.raises(ValueError):
        tb.read_file("r1", "/etc/hosts", 0, 10)
    tb.memory_write("XHS needs login", "use agent-reach for xiaohongshu", "web_fetch gets nothing")
    assert "xhs-needs-login" in tb.memory_index()
    assert "agent-reach" in a.runtime.system_prompt(studio.current_project()["id"], None)
    assert tb.memory_read("xhs-needs-login")["text"].endswith("web_fetch gets nothing\n")
    tb.memory_write("xhs-needs-login", "", "")
    assert tb.memory_index() == ""
    (tb.workdir("r1") / "clip.mp4").write_bytes(b"v")
    up = tb.add_media("r1", "clip.mp4", None)
    assert up["upload"].startswith("@upload:") and up["kind"] == "video"


def test_restart_reports_interrupted_jobs(env):  # noqa: F811
    studio, make, _ = env
    a, _ = make(
        {"tool_calls": [call("run_command", {"command": "sleep 0", "reason": "x"})]},
        {"text": "noted"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    m = pending_approval(studio, run["id"])
    # 模拟：批准后改成等后台结果，但 daemon 在跑完前重启（job 没在这个进程里）
    studio.store.update_agent_msg(m["id"], meta={"key": f"job:{run['id']}:x", "kind": "event"})
    studio.store.update_run(run["id"], status="waiting_event")
    a.runtime.recover()
    run_all(a)
    assert "interrupted" in tool_result(studio, run["id"], "run_command")["error"]
    assert last_run(studio)["status"] == "done"


def test_mcp_read_only_runs_directly_others_need_approval(env):  # noqa: F811
    import sys
    from pathlib import Path

    studio, make, said = env
    studio.orch.cfg.mcp_servers = {
        "fake": {
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "fake_mcp_server.py")],
        }
    }
    a, _script = make(
        {"tool_calls": [call("mcp_list", {})]},
        {
            "tool_calls": [
                call("mcp_call", {"server": "fake", "tool": "search", "args": {"q": "x"}})
            ]
        },
        {
            "tool_calls": [
                call("mcp_call", {"server": "fake", "tool": "post", "args": {"m": 1}}, "c2")
            ]
        },
        {"text": "posted"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    def pump():  # mcp_list、search 在后台跑，各自交回：一直推进到停下等批准
        run_all(a)
        return last_run(studio)["status"] == "waiting_user"

    assert wait_until(pump)
    run = last_run(studio)
    assert "search: Search things" in json.dumps(tool_result(studio, run["id"], "mcp_list"))
    assert run["status"] == "waiting_user"  # post 不是只读：等批准
    assert any("fake.post" in t for t, b in said if b)
    a.runtime.on_approval(pending_approval(studio, run["id"])["id"], True)
    assert wait_until(lambda: run_all(a) or last_run(studio)["status"] == "done")
    texts = [
        json.loads(m["content"])["text"]
        for m in studio.store.agent_msgs(run["id"])
        if m["role"] == "tool" and m["name"] == "mcp_call"
    ]
    assert texts == ['search:{"q": "x"}', 'post:{"m": 1}']
    a.runtime.toolbox.mcp.close()
