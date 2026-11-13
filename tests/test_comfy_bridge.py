import json

from video_agent.agent import Done, Pending, build_tools
from video_agent.comfy_bridge import classify, extract_workflow

from .test_agent import env  # noqa: F401

GRAPH = {"1": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}}}


def test_policy_classification():
    assert classify("list_local_models", {"action": "list"}) == "read"
    assert classify("list_local_models", {"action": "remove"}) == "deny"  # deletes model files
    assert classify("install_custom_node", {"action": "install"}) == "approve"
    assert classify("download_model", {"action": "download"}) == "approve"
    assert classify("restart_comfyui", {"action": "restart"}) == "deny"
    assert classify("queue", {"action": "clear"}) == "deny"  # would touch other people's tasks
    assert classify("clear_vram", {}) == "deny"
    assert classify("create_workflow", {"action": "modify"}) == "read"  # building a workflow
    assert classify("enqueue_workflow", {"action": "enqueue"}) == "run"
    assert classify("generate_image", {"action": "video"}) == "run"
    assert classify("call_tool", {"name": "restart_comfyui", "args": {"action": "stop"}}) == "deny"
    assert classify("call_tool", {"name": "get_history", "args": {"action": "list"}}) == "read"
    # tools added in new versions are not allowed until reviewed
    assert classify("brand_new_tool", {}) == "deny"
    assert classify("comfy_cli", {"action": "models_remove"}) == "deny"


def test_extract_workflow():
    assert extract_workflow("enqueue_workflow", {"action": "enqueue", "workflow": GRAPH}) == GRAPH
    assert extract_workflow("enqueue_workflow", {"workflow": json.dumps(GRAPH)}) == GRAPH
    assert extract_workflow("generate_image", {"prompt": "a cat"}) is None


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return {"result": "ok", "is_error": False}


def test_agent_comfy_tool_routes_by_policy(env):  # noqa: F811
    studio, make, _ = env
    a, _ = make()
    rt = a.runtime
    rt.comfy = FakeBridge()
    tools = build_tools(rt)
    ctx = {"run_id": "r1", "project_id": studio.current_project()["id"], "channel": "web", "origin_text": ""}
    out = tools["comfy"].handler(ctx, {"tool": "list_local_models", "args": {"action": "list"}})
    assert isinstance(out, Done) and rt.comfy.calls == [("list_local_models", {"action": "list"})]
    out = tools["comfy"].handler(ctx, {"tool": "install_custom_node", "args": {"action": "install", "id": "x"}})
    # waits for your approval
    assert isinstance(out, Pending) and out.on_approve["tool"] == "comfy_approved"
    out = tools["comfy"].handler(ctx, {"tool": "restart_comfyui", "args": {"action": "restart"}})
    assert "not allowed" in out.result["error"] and len(rt.comfy.calls) == 1
    out = tools["comfy"].handler(ctx, {"tool": "enqueue_workflow", "args": {"action": "enqueue", "workflow": GRAPH}})
    # not handed to the third party: it became a task in our queue
    rnd = studio.store.get_round(out.result["queued_round"])
    assert rnd["template"] == "comfy-raw" and len(rt.comfy.calls) == 1
    task = studio.store.round_tasks(rnd["id"])[0]
    assert json.loads(task["params"]["graph"]) == GRAPH and task["state"] == "draft"
