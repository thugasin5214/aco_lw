"""测试用的 stdio MCP 服务：search（只读）与 post（有副作用）。"""

import json
import sys

TOOLS = [
    {
        "name": "search",
        "description": "Search things",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "post", "description": "Post a message", "inputSchema": {"type": "object"}},
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}, "capabilities": {}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    else:
        p = msg["params"]
        res = {"content": [{"type": "text", "text": f"{p['name']}:{json.dumps(p['arguments'])}"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
