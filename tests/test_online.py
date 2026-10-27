"""在线后端与本机 agent CLI：转换、回退配置、委托执行、MCP 协议。"""

import json
from types import SimpleNamespace

import pytest
from test_agent import call, env, last_run, run_all  # noqa: F401 — 复用夹具

from video_agent.backends import AnthropicBackend, ChatResult, OpenAICompatBackend
from video_agent.mcp_server import Server


# ---------------- Anthropic 后端 ----------------
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
    }  # id 清洗成 API 接受的字符
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["call_1", "c2"]  # 结果合进一条消息
    assert out[3]["content"][0]["signature"] == "sig"  # Claude 的回合原样回放


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


# ---------------- 回退配置 ----------------
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
    assert script.seen == []  # 本地 LLM 一次都没被调用
    second = online.seen[1]
    assert second[-2]["_anthropic"] == [{"type": "text", "text": "raw"}]  # Claude 的回合原样回放
    logs = [m["text"] for m in studio.store.messages() if m["channel"] == "agent"]
    assert any("local LLM unavailable" in x for x in logs)


# ---------------- 委托给本机 agent CLI ----------------
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
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())  # 同步跑委托线程
    a.inbound("web", "make something", lambda t, b=None: None)
    run_all(a)
    assert script.seen == []
    assert "memory_free_gib" in results["status"]
    assert "exceeds the policy limit" in results["too_many"]["error"]  # 执行策略照样生效
    run = last_run(studio)
    assert run["status"] == "done" and said[-1][0] == "done via Claude Code"
    assert a.runtime.external_tool(run["id"], "get_status", {})["error"].startswith("this run")


def test_cli_commands_are_locked_down(env, tmp_path):  # noqa: F811
    from video_agent.agent_cli import AgentCLI

    studio, _, _ = env
    cmd = AgentCLI("claude_code", studio.orch.cfg).command("r1", "http://x", tmp_path)
    assert cmd[cmd.index("--tools") + 1] == ""  # 没有任何内置工具
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--allowedTools") + 1] == "mcp__aco_lw"
    mcp = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["aco_lw"]
    assert mcp["args"][:2] == ["-m", "video_agent.mcp_server"]
    cx = AgentCLI("codex", studio.orch.cfg).command("r1", "http://x", tmp_path)
    assert cx[cx.index("-s") + 1] == "read-only" and cx[-1] == "-"


# ---------------- MCP 协议 ----------------
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
