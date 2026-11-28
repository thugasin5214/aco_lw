"""Generic tool layer: the agent proposes → the orchestrator executes (commands approved first, run in the background) → results go back to the LLM as memory allows."""

import json
import time
from pathlib import Path

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — env is a fixture

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
    assert run["status"] == "waiting_user"  # waiting for your approval; the command has not run
    assert any("echo hi" in t and b for t, b in said)  # the command shown verbatim + buttons
    m = pending_approval(studio, run["id"])
    a.runtime.on_approval(m["id"], True)
    # done: a step is queued to hand the result to the LLM
    assert wait_until(lambda: a.jobs.qsize() > 0)
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
    assert not any(b for _, b in said)  # no approval buttons were sent
    assert blocked("cat ~/x/FreeVideo-local/agent/token")
    assert blocked("pkill -f ComfyUI")
    assert blocked("lms unload --all") is None  # LLM load / unload: approved like any command
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
    """The LLM is unloaded when the command finishes (a film is shooting): the result is parked and handed back once memory frees up."""
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
    assert "late" in tool_result(studio, run["id"], "run_command")["stdout"]  # stored
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
    # @upload:ID works as a path
    assert tb.readable("r1", up["upload"]) == Path(up["path"]).resolve()
    with pytest.raises(ValueError):
        tb.readable("r1", "@task:nope")


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
    # simulate: after approval it switches to waiting for the background result, but the daemon
    # restarts before it finishes (the job is not in this process)
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

    # mcp_list and search run in the background and report back each: advance until it stops for
    # approval
    def pump():
        run_all(a)
        return last_run(studio)["status"] == "waiting_user"

    assert wait_until(pump)
    run = last_run(studio)
    assert "search: Search things" in json.dumps(tool_result(studio, run["id"], "mcp_list"))
    assert run["status"] == "waiting_user"  # post is not read-only: waits for approval
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


def test_look_sends_images_to_the_vision_llm(env):  # noqa: F811
    """look: the orchestrator shrinks images and grabs video frames, handing them to the LLM as images right after the tool result."""
    import subprocess

    studio, make, _ = env
    a, script = make(
        {"tool_calls": [call("look", {"paths": ["a.png", "clip.mp4", "nope.png"]})]},
        {"text": "the face is round"},
    )
    a.inbound("web", "check it", lambda t, b=None: None)
    run = last_run(studio)
    wd = a.runtime.toolbox.workdir(run["id"])
    for cmd in (
        ["-f", "lavfi", "-i", "color=red:s=2000x1000", "-frames:v", "1", str(wd / "a.png")],
        ["-f", "lavfi", "-i", "testsrc=s=320x240:d=2", str(wd / "clip.mp4")],
    ):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *cmd], check=True)
    assert wait_until(lambda: run_all(a) or last_run(studio)["status"] == "done", 30)
    res = tool_result(studio, run["id"], "look")
    assert len(res["images"]) == 2 and res["errors"][0]["ref"] == "nope.png"
    msgs = script.seen[-1]
    i = next(k for k, m in enumerate(msgs) if m["role"] == "tool")
    pics = msgs[i + 1]
    assert pics["role"] == "user"
    urls = [p["image_url"]["url"] for p in pics["content"] if p["type"] == "image_url"]
    assert len(urls) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in urls)
    assert any("6 frames" in p.get("text", "") for p in pics["content"])


def test_claude_backend_converts_image_parts():
    from video_agent.backends import AnthropicBackend

    _, out, _ = AnthropicBackend.convert(
        [
            {"role": "system", "content": "s"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "[look] x"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
                ],
            },
        ],
        None,
    )
    assert out[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
    }


def test_always_allow_rule_skips_the_next_approval(env):  # noqa: F811
    studio, make, said = env
    a, _ = make(
        {"tool_calls": [call("run_command", {"command": "echo one", "reason": "x"})]},
        {"tool_calls": [call("run_command", {"command": "echo two", "reason": "x"}, "c2")]},
        {"tool_calls": [call("run_command", {"command": "echo a | cat", "reason": "x"}, "c3")]},
        {"text": "done"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    buttons = next(b for t, b in said if b)
    assert (
        "✅ Always (cmd:echo)",
        f"a:{pending_approval(studio, run['id'])['id']}:always",
    ) in buttons[0]
    a.runtime.on_approval(pending_approval(studio, run["id"])["id"], True, remember=True)
    assert a.runtime.allow_rules() == ["cmd:echo"]

    def pump():
        run_all(a)
        return last_run(studio)["status"] == "waiting_user"

    assert wait_until(pump)  # echo two is auto-allowed; the piped echo | cat still asks
    texts = [
        json.loads(m["content"])
        for m in studio.store.agent_msgs(run["id"])
        if m["role"] == "tool" and m["status"] == "done"
    ]
    assert "two" in texts[-1]["stdout"]
    done = [m for m in studio.store.agent_msgs(run["id"]) if m["role"] == "tool"]
    assert done[1]["meta"]["info"]["auto"] == "rule"
    assert (
        "echo a | cat"
        in pending_approval(studio, run["id"])["meta"]["on_approve"]["args"]["command"]
    )


def test_bypass_mode_runs_without_asking_but_hard_refusals_stay(env):  # noqa: F811
    studio, make, said = env
    studio.store.set_kv("permission_mode", "bypass")
    a, _ = make(
        {"tool_calls": [call("run_command", {"command": "echo hi > f.txt", "reason": "x"})]},
        {"tool_calls": [call("run_command", {"command": "pkill -f ComfyUI", "reason": "x"}, "c2")]},
        {"text": "done"},
    )
    a.inbound("web", "go", lambda t, b=None: None)
    assert wait_until(lambda: run_all(a) or last_run(studio)["status"] == "done")
    run = last_run(studio)
    res = [
        json.loads(m["content"]) for m in studio.store.agent_msgs(run["id"]) if m["role"] == "tool"
    ]
    assert res[0]["exit_code"] == 0
    first = next(m for m in studio.store.agent_msgs(run["id"]) if m["role"] == "tool")
    assert first["meta"]["info"]["auto"] == "bypass"
    assert "refused" in res[1]["error"]
    assert not any(b for _, b in said)  # no approval buttons at all
    assert any("Auto-approved" in t for t, _ in said)


def test_permissions_command(env):  # noqa: F811
    _studio, make, _ = env
    a, _ = make()
    out = []
    say = lambda t, b=None: out.append(t)
    a.inbound("web", "/permissions bypass", say)
    assert a.runtime.permission_mode() == "bypass"
    a.runtime.remember_rule("cmd:ffmpeg")
    a.inbound("web", "/permissions", say)
    assert "bypass" in out[-1] and "cmd:ffmpeg" in out[-1]
    a.inbound("web", "/permissions forget all", say)
    a.inbound("web", "/permissions default", say)
    assert a.runtime.allow_rules() == [] and a.runtime.permission_mode() == "default"


def test_command_output_cut_inside_a_utf8_char_does_not_fail(env):  # noqa: F811
    _studio, make, _said = env
    a, _ = make()
    # "中" is 3 bytes and only the first 2 are printed: this used to fail the whole command with
    # UnicodeDecodeError
    res = a.runtime.toolbox.run_command("r-utf8", "printf '中文\\xe4\\xb8'", 10)
    assert res["exit_code"] == 0 and res["stdout"].startswith("中文")
