import tomllib

import pytest
from fastapi.testclient import TestClient
from test_sessions import actions_with, plan_reply, studio  # noqa: F401
from test_studio import run_jobs

from video_agent import config
from video_agent.studio import StudioError

TOML = """comfy_output_dir = "~/x"  # 注释保留
telegram_chat_ids = [
  1,
  2,
]
llm_context = 4096

[mcp_servers.exa]
url = "https://mcp.exa.ai/mcp"
"""


def test_save_keeps_other_keys_tables_and_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(TOML)
    config.save({"llm_context": 65536, "telegram_chat_ids": [5], "llm_gpu": "max"}, p)
    text = p.read_text()
    raw = tomllib.loads(text)
    assert raw["llm_context"] == 65536 and raw["telegram_chat_ids"] == [5]
    assert raw["llm_gpu"] == "max" and raw["mcp_servers"]["exa"]["url"].startswith("https")
    assert "# 注释保留" in text and raw["comfy_output_dir"] == "~/x"
    assert text.index("llm_gpu") < text.index("[mcp_servers.exa]")  # 新键进顶层，不进表里


def test_coerce_types_and_rejects():
    assert config.coerce("llm_context", "8192") == 8192
    assert config.coerce("allow_online", "true") is True
    assert config.coerce("llm_fallback", "anthropic, codex") == ["anthropic", "codex"]
    assert config.coerce("telegram_chat_ids", "1,2") == [1, 2]
    assert config.coerce("llm_gpu", "0.5") == "0.5"
    with pytest.raises(ValueError):
        config.coerce("llm_gpu", "2")
    with pytest.raises(ValueError):
        config.coerce("data_dir", "/tmp")  # 不在可编辑列表里
    with pytest.raises(ValueError):
        config.coerce("language", "fr")


def test_config_api_masks_secrets_and_applies_live(studio, tmp_path):  # noqa: F811
    from video_agent.web import create_app

    a, *_ = actions_with(studio, [])
    cfg = studio.orch.cfg
    cfg.openai_api_key = "sk-secret"
    cfg.config_path = tmp_path / "config.toml"
    cfg.config_path.write_text(TOML)
    c = TestClient(create_app(cfg, a))
    got = c.get("/api/config").json()
    key = next(f for f in got["fields"] if f["name"] == "openai_api_key")
    assert key["is_set"] and "value" not in key and "sk-secret" not in str(got)
    r = c.post(
        "/api/config",
        json={
            "values": {"agent_max_steps": "12", "openai_api_key": "", "round_size": 3},
            "runtime": {"permission_mode": "bypass"},
        },
    ).json()
    assert r["saved"] == ["agent_max_steps", "round_size"] and r["restart"] == ["round_size"]
    assert a.runtime.max_steps == 12 and cfg.openai_api_key == "sk-secret"  # 空密钥 = 不改
    assert a.runtime.permission_mode() == "bypass"
    assert tomllib.loads(cfg.config_path.read_text())["agent_max_steps"] == 12
    assert c.post("/api/config", json={"values": {"data_dir": "/"}}).status_code == 400


def test_delete_session_removes_history_and_switches(studio):  # noqa: F811
    a, _planner, _out, reply = actions_with(studio, [plan_reply(), plan_reply()])
    a.inbound("telegram", "第一句", reply)
    run_jobs(a)
    first = studio.current_session()
    run = studio.store.create_run(first["project_id"], first["id"], "telegram", "x")
    studio.store.add_agent_msg(run["id"], "user", "x")
    with pytest.raises(StudioError):  # agent 还在这个会话里跑
        studio.delete_session(first["id"])
    studio.store.update_run(run["id"], status="done")
    second = studio.new_session("第二个")
    studio.use_session(first["id"])
    res = studio.delete_session(first["id"])
    assert res["messages"] >= 2 and res["runs"] == 1
    assert studio.store.get_session(first["id"]) is None
    assert studio.store.runs(first["id"]) == [] and studio.store.agent_msgs(run["id"]) == []
    assert studio.current_session()["id"] == second["id"]  # 删的是当前会话：换到剩下的


def test_session_delete_command_asks_then_deletes(studio):  # noqa: F811
    a, _p, out, reply = actions_with(studio, [plan_reply()])
    a.inbound("telegram", "你好", reply)
    run_jobs(a)
    sid = studio.current_session()["id"]
    assert studio.store.sessions(studio.current_project()["id"])[0]["n_telegram"] >= 1
    a.inbound("telegram", "/session delete 1", reply)
    _text, buttons = out[-1]
    assert buttons[0][0][1] == f"k:{sid}"  # 先问，确认按钮才删
    assert studio.store.get_session(sid)
    a.button("telegram", f"k:{sid}", reply)
    assert studio.store.get_session(sid) is None


def test_local_backend_follows_settings(monkeypatch):
    from video_agent import llm
    from video_agent.backends import LMStudioBackend
    from video_agent.config import Settings

    cfg = Settings(llm_model="a", llm_context=1000)
    b = LMStudioBackend("http://x", "a", 1000, lambda: 60, cfg=cfg)
    cfg.llm_model, cfg.llm_context, cfg.llm_gpu, cfg.llm_parallel = "b", 8192, "max", 2
    calls = []
    monkeypatch.setattr(llm, "loaded", lambda m: None)
    monkeypatch.setattr(llm, "_lms", lambda *a: calls.append(a) or "")
    b.prepare()
    assert b.model == "b"
    assert calls[0] == (
        "load",
        "b",
        "-y",
        "--identifier",
        "b",
        "-c",
        "8192",
        "--ttl",
        "60",
        "--gpu",
        "max",
        "--parallel",
        "2",
    )
