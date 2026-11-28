"""aco-bench: cases load, a run exports to the VAHBench layout, aco's metrics come out of the
database, the report table is built, and the scripted user answers and steers."""

import json
import shutil
import subprocess
import time

import pytest

from video_agent.bench import cases, export, metrics, report
from video_agent.bench.runner import Runner
from video_agent.studio import Studio

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_shipped_cases_load():
    ids = [c.id for c in cases.all_cases()]
    assert "s01-red-umbrella" in ids and len(ids) >= 6
    c = cases.load("s01-red-umbrella")
    assert c.folder == "prompt_s01_red_umbrella" and c.user.steer[0].after == "first_video"
    assert "send_file" in c.delivery_note()


def make_video(path, seconds=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=12:d={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def fake_run(tmp_path, studio: Studio):
    """A finished run: queued a draft and a 4x bigger render without looking, sent one file."""
    store = studio.store
    project = studio.new_project("bench test")
    pid = project["id"]
    run = store.create_run(pid, None, "web", "make it")
    small = studio.start_round("flux2-t2i", {"prompt": "x"}, 1, None, "draft", project_id=pid)
    big = studio.start_round("flux2-t2i", {"prompt": "x"}, 1, None, "hd", project_id=pid)
    for rnd, (w, cost_s) in ((small, (320, 30.0)), (big, (1280, 300.0))):
        t = store.round_tasks(rnd["id"])[0]
        store.update(
            t["id"],
            kind="video",
            params={"width": w, "height": w, "length": 49},
            metrics={"exec_s": cost_s, "memory": {"peak_used_gib": 50, "max_pressure": 2}},
        )
        time.sleep(0.01)
    video = make_video(tmp_path / "final.mp4")
    calls = [
        {"id": "c1", "name": "get_status", "arguments": "{}"},
        {"id": "c2", "name": "get_status", "arguments": "{}"},
    ]
    store.add_agent_msg(run["id"], "user", "## User\nmake it")
    store.add_agent_msg(run["id"], "assistant", None, tool_calls=calls, seconds=12.5)
    for c in calls:
        store.add_agent_msg(run["id"], "tool", '{"ok": true}', tool_call_id=c["id"], name=c["name"])
    send = [{"id": "c3", "name": "send_file", "arguments": json.dumps({"path": str(video)})}]
    store.add_agent_msg(run["id"], "assistant", None, tool_calls=send, seconds=3.0)
    store.add_agent_msg(
        run["id"], "tool", json.dumps({"sent": str(video)}), tool_call_id="c3", name="send_file"
    )
    store.add_agent_msg(
        run["id"],
        "tool",
        '{"error": "not run: you made this exact call 3 times"}',
        tool_call_id="ext_1",
        name="get_round",
    )
    store.add_agent_msg(run["id"], "assistant", "Done: one 2 s film.", seconds=2.0)
    store.update_run(run["id"], status="done")
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    case = cases.load("s01-red-umbrella").model_dump()
    (run_dir / "run.json").write_text(
        json.dumps({"case": case, "brain": "local", "project_id": pid, "wall_s": 600})
    )
    steer_ts = time.time() - 5
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {"t": 10, "ts": steer_ts, "kind": "user", "text": "night rain", "why": "steer 0"}
        )
        + "\n"
    )
    return run_dir, pid


@needs_ffmpeg
def test_export_and_metrics(tmp_path, make_orch, fake):
    studio = Studio(make_orch(fake), round_size=1)
    run_dir, _ = fake_run(tmp_path, studio)
    dest = export.export(run_dir, studio.store, tmp_path / "export")
    assert dest == tmp_path / "export" / "aco" / "local" / "prompt_s01_red_umbrella"
    events = [json.loads(x) for x in (dest / "events.jsonl").read_text().splitlines()]
    # VAHBench "hermes" shape: assistant tool_calls[].function + tool results by tool_call_id
    assert events[1]["tool_calls"][0]["function"]["name"] == "get_status"
    # a delegated agent's call has no assistant turn of ours: one is synthesised for it
    assert any(
        e["role"] == "assistant" and e.get("tool_calls", [{}])[0].get("id") == "ext_1"
        for e in events
    )
    ver = json.loads((dest / "verification.json").read_text())
    assert ver["episodes"][0]["file"] == "out/episode1.mp4" and ver["episodes"][0]["seconds"] == 2.0
    assert json.loads((dest / "run.json").read_text())["target"]["seconds_per_episode"] == 30

    m = metrics.collect(run_dir, studio.store, {})
    assert m["time"]["llm_s"] == 17.5 and m["time"]["render_s"] == 330.0
    assert m["time"]["render_s_per_delivered_s"] == 165.0
    assert m["loops"]["max_identical_in_row"] == 2 and m["loops"]["refused_repeats"] == 1
    assert m["self_review"]["scaled_without_check"] == 1  # 16x the cost, no look in between
    assert m["steering"][0]["first_turn_s"] is not None
    assert m["final_reply"] == "Done: one 2 s film."

    (run_dir / "score.json").write_text(json.dumps(m))
    rows, md = report.build(tmp_path / "runs")
    assert rows[0]["render_min"] == 5.5 and "| s01-red-umbrella | local |" in md


def test_lora_family_and_mismatch():
    task = {
        "template": "comfy-raw",
        "params": {
            "graph": json.dumps(
                {
                    "1": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}},
                    "2": {
                        "class_type": "LoraLoaderModelOnly",
                        "inputs": {"lora_name": "a.safetensors", "strength_model": 0.8},
                    },
                }
            )
        },
    }
    used = metrics.loras_of(task)
    assert used == [{"lora": "a.safetensors", "strength": 0.8, "family": "minimax-h3"}]


class StubRunner(Runner):
    """The runner's user logic without a daemon: posts are recorded."""

    def __init__(self, case, store, project_id, tmp_path):
        self.case, self.brain, self.store, self.project_id = case, "local", store, project_id
        self.dir = tmp_path
        self.t0 = time.time()
        self.fired, self.answered, self.posts = set(), set(), []

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


def test_scripted_user_answers_and_steers(tmp_path, make_orch, fake):
    studio = Studio(make_orch(fake), round_size=1)
    pid = studio.new_project("p")["id"]
    run = studio.store.create_run(pid, None, "web", "x")
    studio.store.add_agent_msg(
        run["id"],
        "tool",
        None,
        tool_call_id="a1",
        name="run_command",
        status="pending",
        kind="approval",
        key="approval:1",
        bypassable=True,
    )
    studio.store.add_agent_msg(
        run["id"],
        "tool",
        None,
        tool_call_id="q1",
        name="ask_user",
        status="pending",
        kind="user",
        key=f"ask:{run['id']}",
    )
    r = StubRunner(cases.load("s01-red-umbrella"), studio.store, pid, tmp_path)
    r._answer_pending()
    assert any(p == "/api/action" and b["data"].endswith(":yes") for p, b in r.posts)
    assert any(p == "/api/chat" and b["text"] == r.case.user.answer for p, b in r.posts)
    r._answer_pending()  # answered once only
    assert len(r.posts) == 2
    # the steer waits for the first video
    r._steer()
    assert len(r.posts) == 2
    rnd = studio.start_round("flux2-t2i", {"prompt": "x"}, 1, None, "t", project_id=pid)
    t = studio.store.round_tasks(rnd["id"])[0]
    studio.store.update(t["id"], kind="video", outputs=[{"file": "v.mp4", "kind": "video"}])
    r._steer()
    assert r.posts[-1] == ("/api/chat", {"text": r.case.user.steer[0].say})
