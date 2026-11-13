"""Online backends and local agent CLIs: conversion, fallback config, delegated execution, MCP protocol."""

import json
from types import SimpleNamespace

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — reused fixtures

from video_agent.backends import AnthropicBackend, ChatResult, OpenAICompatBackend
from video_agent.mcp_server import Server


# ---------------- Anthropic backend ----------------
def test_anthropic_convert_groups_results_and_replays_raw():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "local thinking",
            "tool_calls": [
                {
                    "id": "call:1",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "get_round", "arguments": '{"round_id": "r"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call:1", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        {
            "role": "assistant",
            "content": "x",
            "_anthropic": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "x"},
            ],
        },
        {"role": "user", "content": "next"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_status",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    system, out, atools = AnthropicBackend.convert(msgs, tools)
    assert system == "SYS" and atools[0]["input_schema"]["type"] == "object"
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out[1]["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_status",
        "input": {},
    }  # ids cleaned to characters the API accepts
    # results merged into one message
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["call_1", "c2"]
    assert out[3]["content"][0]["signature"] == "sig"  # Claude's turn replayed unchanged


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def test_anthropic_chat_maps_response_and_cache_usage():
    sent = {}

    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(
            model="claude-opus-5-5",
            stop_reason="tool_use",
            stop_details=None,
            content=[
                _Block(type="thinking", thinking="", signature="s"),
                _Block(type="text", text="checking"),
                _Block(type="tool_use", id="tu_1", name="get_status", input={}),
            ],
            usage=SimpleNamespace(
                input_tokens=50,
                output_tokens=20,
                cache_read_input_tokens=3000,
                cache_creation_input_tokens=0,
            ),
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    b = AnthropicBackend(client=client)
    res = b.chat(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": "",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )
    assert res.tool_calls == [{"id": "tu_1", "name": "get_status", "arguments": "{}"}]
    assert res.prompt_tokens == 3050 and res.cached_tokens == 3000 and res.cache_hit
    assert res.raw_content[0]["type"] == "thinking"
    assert sent["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["betas"]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["output_config"] == {"effort": "medium"}


def test_openai_compatible_strips_private_and_local_fields():
    b = OpenAICompatBackend("https://example.invalid", "m")
    body = b._body(
        [{"role": "assistant", "content": "x", "reasoning_content": "r", "_anthropic": []}],
        None,
        10,
    )
    assert body["messages"] == [{"role": "assistant", "content": "x"}]


# ---------------- fallback config ----------------
class FakeOnline:
    name, local = "anthropic", False

    def __init__(self, *outs):
        self.outs, self.seen = list(outs), []

    def chat(self, messages, schema=None, max_tokens=4096, tools=None):
        self.seen.append(messages)
        o = self.outs.pop(0)
        return ChatResult(
            text=o.get("text", ""),
            tool_calls=o.get("tool_calls", []),
            backend="anthropic:claude-opus-5-5",
            raw_content=[{"type": "text", "text": "raw"}],
        )


def busy_video(studio):
    studio.orch.resources.render_active = {
        "task_id": "x",
        "family": "freevideo-h3",
        "kind": "video",
        "exclusive": True,
    }


def test_fallback_needs_allow_online(env):  # noqa: F811
    studio, make, _ = env
    studio.orch.cfg.llm_fallback = ["anthropic"]
    busy_video(studio)
    a, _script = make({"text": "local"})
    a.inbound("web", "hello", lambda t, b=None: None)
    run_all(a)
    run = last_run(studio)
    assert (
        run["status"] == "waiting_resources" and "allow_online is off" in run["waiting"]["reason"]
    )


def test_online_api_takes_over_when_local_is_busy(env):  # noqa: F811
    studio, make, said = env
    studio.orch.cfg.llm_fallback = ["anthropic"]
    studio.orch.cfg.allow_online = True
    busy_video(studio)
    a, script = make()
    online = FakeOnline({"tool_calls": [call("get_status")]}, {"text": "answered online"})
    a.runtime._api_backends = {"anthropic": online}
    a.inbound("web", "status?", lambda t, b=None: None)
    run_all(a)
    assert last_run(studio)["status"] == "done" and said[-1][0] == "answered online"
    assert script.seen == []  # the local LLM was never called
    second = online.seen[1]
    # Claude's turn replayed unchanged
    assert second[-2]["_anthropic"] == [{"type": "text", "text": "raw"}]
    logs = [m["text"] for m in studio.store.messages() if m["channel"] == "agent"]
    assert any("local LLM unavailable" in x for x in logs)


# ---------------- delegating to a local agent CLI ----------------
def test_delegate_to_cli_agent_runs_tools_through_policy(env, monkeypatch):  # noqa: F811
    studio, make, said = env
    studio.orch.cfg.llm_fallback = ["claude_code"]
    studio.orch.cfg.allow_online = True
    busy_video(studio)
    a, script = make()
    results = {}

    class FakeCLI:
        def run(self, run_id, prompt, url):
            assert "Use only the aco_lw tools" in prompt and "## User" in prompt
            results["status"] = a.runtime.external_tool(run_id, "get_status", {})
            results["too_many"] = a.runtime.external_tool(
                run_id, "queue_round", {"template": "flux2-t2i", "prompt": "x", "n": 20}
            )
            return "done via Claude Code", {"via": "claude_code", "cost_usd": 0.01}

    a.runtime.cli_runner = lambda name: FakeCLI()
    # run the delegation thread synchronously
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())
    a.inbound("web", "make something", lambda t, b=None: None)
    run_all(a)
    assert script.seen == []
    assert "memory_free_gib" in results["status"]
    # execution policy still applies
    assert "exceeds the policy limit" in results["too_many"]["error"]
    run = last_run(studio)
    assert run["status"] == "done" and said[-1][0] == "done via Claude Code"
    assert a.runtime.external_tool(run["id"], "get_status", {})["error"].startswith("this run")


def test_cli_commands_are_locked_down(env, tmp_path):  # noqa: F811
    from video_agent.agent_cli import AgentCLI

    studio, _, _ = env
    cmd = AgentCLI("claude_code", studio.orch.cfg).command("r1", "http://x", tmp_path)
    assert cmd[cmd.index("--tools") + 1] == ""  # no built-in tools at all
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--allowedTools") + 1] == "mcp__aco_lw"
    mcp = json.loads((tmp_path / "r1.mcp.json").read_text())["mcpServers"]["aco_lw"]
    assert mcp["args"][:2] == ["-m", "video_agent.mcp_server"]
    cx = AgentCLI("codex", studio.orch.cfg).command("r1", "http://x", tmp_path)
    assert cx[cx.index("-s") + 1] == "read-only" and cx[-1] == "-"


# ---------------- MCP protocol ----------------
class FakeHTTP:
    def __init__(self):
        self.posts = []

    def get(self, url):
        return SimpleNamespace(
            json=lambda: [
                {"name": "get_status", "description": "d", "parameters": {"type": "object"}}
            ]
        )

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return SimpleNamespace(
            json=lambda: {"error": "nope"} if json["name"] == "bad" else {"ok": 1}
        )


def test_mcp_server_protocol():
    http = FakeHTTP()
    srv = Server("run1", "http://d", http=http)
    init = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert init["result"]["capabilities"] == {"tools": {}}
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert tools[0]["inputSchema"] == {"type": "object"}
    ok = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}},
        }
    )["result"]
    assert json.loads(ok["content"][0]["text"]) == {"ok": 1} and not ok["isError"]
    bad = srv.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bad"}}
    )["result"]
    assert bad["isError"] and http.posts[0]["run_id"] == "run1"
    assert srv.handle({"jsonrpc": "2.0", "id": 5, "method": "x"})["error"]["code"] == -32601


def test_agent_tool_endpoint_only_for_delegated_runs(env):  # noqa: F811
    from fastapi.testclient import TestClient

    from video_agent.web import create_app

    studio, make, _ = env
    a, _ = make({"text": "hi"})
    a.inbound("web", "hi", lambda t, b=None: None)
    run_all(a)
    c = TestClient(create_app(studio.orch.cfg, a))
    assert any(t["name"] == "queue_round" for t in c.get("/api/agent/tools").json())
    r = c.post("/api/agent/tool", json={"run_id": last_run(studio)["id"], "name": "get_status"})
    assert "not delegated" in r.json()["error"]
    pytest.importorskip("anthropic")


def test_openrouter_backend(env, monkeypatch):  # noqa: F811
    import httpx

    from video_agent.planner import PlannerUnavailable, make_named_backend

    studio, _, _ = env
    cfg = studio.orch.cfg
    with pytest.raises(PlannerUnavailable, match="openrouter_model"):
        make_named_backend(cfg, studio.store, "openrouter")
    cfg.openrouter_model = "vendor/model"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    b = make_named_backend(cfg, studio.store, "openrouter")
    assert b.name == "openrouter" and b.base_url == "https://openrouter.ai/api" and not b.local
    seen = {}

    def handler(req):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "t1",
                                    "function": {"name": "get_status", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 800},
                },
            },
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body)

    b.http = httpx.Client(transport=httpx.MockTransport(handler), headers=b.http.headers)
    res = b.chat([{"role": "user", "content": "x", "reasoning_content": "r"}], tools=[])
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert (
        seen["auth"] == "Bearer k-test" and "reasoning_content" not in seen["body"]["messages"][0]
    )
    assert res.tool_calls[0]["name"] == "get_status" and res.cached_tokens == 800
    assert res.backend == "openrouter"


def test_codex_error_is_read_from_event_stream():
    from video_agent.agent_cli import AgentCLI

    out = '{"type":"thread.started"}\n{"type":"turn.failed","error":{"message":"{\\"error\\":{\\"message\\":\\"model not supported\\"}}"}}'
    assert AgentCLI._codex_error(out) == "model not supported"
    assert AgentCLI._codex_error('{"type":"turn.completed"}') is None


# ---------------- handing off to the FreeVideo agent ----------------
def test_freevideo_agent_cli_gets_both_mcp_servers(env, tmp_path):  # noqa: F811
    from video_agent.agent_cli import AgentCLI

    studio, _, _ = env
    fv = {"freevideo": {"command": "/py", "args": ["-m", "freevideo_engine.agent.mcp"]}}
    cmd = AgentCLI("freevideo", studio.orch.cfg, fv).command("r1", "http://x", tmp_path)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__aco_lw mcp__freevideo"
    servers = json.loads((tmp_path / "r1.mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"aco_lw", "freevideo"}


def test_fv_command_needs_its_online_switch_then_hands_over(env, monkeypatch):  # noqa: F811
    studio, make, said = env
    a, script = make()
    seen = {}

    class FakeCLI:
        def run(self, run_id, prompt, url):
            seen["prompt"] = prompt
            return "made it in FreeVideo", {"via": "freevideo"}

    a.runtime.cli_runner = lambda name: seen.setdefault("name", name) and FakeCLI()
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())
    replies = []
    a.inbound("web", "/fv dance video", lambda t, b=None: replies.append(t))
    # not enabled: nothing goes out
    assert "freevideo_agent_online" in replies[-1] and "name" not in seen
    studio.orch.cfg.freevideo_agent_online = True  # allow online for this route only
    assert studio.orch.cfg.allow_online is False
    a.inbound("web", "/fv dance video", lambda t, b=None: None)
    assert seen["name"] == "freevideo" and "FreeVideo agent" in seen["prompt"]
    assert "dance video" in seen["prompt"] and script.seen == []
    assert said[-1][0] == "made it in FreeVideo" and last_run(studio)["status"] == "done"


def test_local_agent_hands_over_after_approval_and_gets_the_report(env, monkeypatch):  # noqa: F811
    studio, make, said = env
    studio.orch.cfg.freevideo_agent_online = True
    a, _script = make(
        {"tool_calls": [call("delegate_freevideo", {"task": "make a cast card film"})]},
        lambda msgs: {"text": "FreeVideo says: " + json.loads(msgs[-1]["content"])["reply"]},
    )

    class FakeCLI:
        def run(self, run_id, prompt, url):
            return "film ready", {"via": "freevideo"}

    a.runtime.cli_runner = lambda name: FakeCLI()
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())
    a.inbound("web", "make a film with cast cards", lambda t, b=None: None)
    run_all(a)
    parent = studio.store.runs(None)[0]
    assert parent["status"] == "waiting_user"  # asks whether you agree to hand it over first
    m = next(
        x
        for x in studio.store.agent_msgs(parent["id"])
        if x["role"] == "tool" and x["status"] != "done"
    )
    a.runtime.on_approval(m["id"], True)
    run_all(a)
    runs = {r["id"]: r for r in studio.store.runs(None)}
    child = next(r for r in runs.values() if r["id"] != parent["id"])
    assert child["status"] == "done" and child["origin_text"] == "make a cast card film"
    assert runs[parent["id"]]["status"] == "done"
    assert said[-1][0] == "FreeVideo says: film ready"


# ---------------- online brains: /model switching, the official OpenAI API, handing to Claude Code
# / Codex ----------------
def test_openai_backend_uses_reasoning_model_params(env, monkeypatch):  # noqa: F811
    from video_agent.planner import PlannerUnavailable, make_named_backend

    studio, _, _ = env
    cfg = studio.orch.cfg
    cfg.openai_model = "gpt-test"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(PlannerUnavailable, match="API key"):
        make_named_backend(cfg, studio.store, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    b = make_named_backend(cfg, studio.store, "openai")
    assert b.base_url == "https://api.openai.com" and b.name == "openai"
    body = b._body([{"role": "user", "content": "x"}], None, 100)
    assert body["max_completion_tokens"] == 100 and "temperature" not in body
    assert "max_tokens" not in body


def test_model_command_switches_brain_only_when_online_is_allowed(env):  # noqa: F811
    studio, make, said = env
    a, script = make()
    online = FakeOnline({"text": "from openrouter"})
    a.runtime._api_backends = {"openrouter": online}
    replies = []
    reply = lambda t, b=None: replies.append(t)
    a.inbound("web", "/model openrouter", reply)
    assert "allow_online" in replies[-1] and a.runtime.brain() == "local"
    studio.orch.cfg.allow_online = True
    a.inbound("web", "/model nope", reply)
    assert "nope" in replies[-1]
    a.inbound("web", "/model openrouter", reply)
    assert a.runtime.brain() == "openrouter"
    a.inbound("web", "hi", lambda t, b=None: None)
    run_all(a)
    assert said[-1][0] == "from openrouter" and script.seen == []  # local was not called
    # then online is turned off: automatically back to local, nothing goes out
    studio.orch.cfg.allow_online = False
    script.steps.append({"text": "local again"})
    a.inbound("web", "hi", lambda t, b=None: None)
    run_all(a)
    assert said[-1][0] == "local again"


def test_model_codex_hands_whole_request_to_the_cli(env, monkeypatch):  # noqa: F811
    studio, make, said = env
    studio.orch.cfg.allow_online = True
    a, script = make()
    seen = {}

    class FakeCLI:
        def run(self, run_id, prompt, url):
            return "codex did it", {"via": "codex"}

    a.runtime.cli_runner = lambda name: seen.setdefault("name", name) and FakeCLI()
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())
    a.inbound("web", "/model codex", lambda t, b=None: None)
    a.inbound("web", "research this", lambda t, b=None: None)
    run_all(a)
    assert seen["name"] == "codex" and script.seen == [] and said[-1][0] == "codex did it"


def test_local_agent_delegates_subtask_to_claude_code_after_approval(env, monkeypatch):  # noqa: F811
    studio, make, said = env
    a, _script = make(
        {"tool_calls": [call("delegate_agent", {"agent": "claude_code", "task": "debug it"})]},
        lambda msgs: {"text": "noted: " + json.loads(msgs[-1]["content"])["error"]},
    )
    a.inbound("web", "hard bug", lambda t, b=None: None)
    run_all(a)
    # online not enabled: no approval prompt, the agent is told directly
    assert said[-1][0].startswith("noted: Online")
    studio.orch.cfg.allow_online = True
    a2, _ = make(
        {"tool_calls": [call("delegate_agent", {"agent": "claude_code", "task": "debug it"})]},
        lambda msgs: {"text": "CC says: " + json.loads(msgs[-1]["content"])["reply"]},
    )

    class FakeCLI:
        def run(self, run_id, prompt, url):
            return "fixed", {"via": "claude_code"}

    names = []
    a2.runtime.cli_runner = lambda name: names.append(name) or FakeCLI()
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())
    a2.inbound("web", "hard bug", lambda t, b=None: None)
    run_all(a2)
    parent = last_run(studio)
    assert parent["status"] == "waiting_user" and any("claude_code" in t for t, b in said if b)
    m = next(
        x
        for x in studio.store.agent_msgs(parent["id"])
        if x["role"] == "tool" and x["status"] != "done"
    )
    a2.runtime.on_approval(m["id"], True)
    run_all(a2)
    assert names == ["claude_code"] and said[-1][0] == "CC says: fixed"
