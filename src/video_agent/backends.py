"""规划器后端（S9）：会话、记忆、缓存逻辑与具体模型服务无关。

- `OpenAICompatBackend`：任何 OpenAI 兼容的 /v1/chat/completions（云端或本地）。
- `LMStudioBackend`：在其上加本地模型的加载 / TTL / 预热管理（G0：加载约 15 s；前缀缓存实测有效）。
- `FuncBackend`：测试用，包装一个函数。
阶段 6 的 Anthropic / agent CLI 后端实现同一接口即可。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from . import llm


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict] = field(default_factory=list)  # [{id, name, arguments(str)}]
    reasoning: str = ""  # 模型的思考内容：同一次运行内回放它，历史才与生成时逐字一致（缓存）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_s: float = 0.0
    total_s: float = 0.0
    load_s: float = 0.0

    @property
    def cached_tokens_est(self) -> int:
        """估算复用的前缀 token：总数减去首 token 时间内按冷算速度（G0 实测约 360 token/s）能算完的量。"""
        return max(0, int(self.prompt_tokens - self.ttft_s * 360))

    @property
    def cache_hit(self) -> bool:
        return self.prompt_tokens > 0 and self.cached_tokens_est > self.prompt_tokens * 0.5

    def stats(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "ttft_s": round(self.ttft_s, 2),
            "seconds": round(self.total_s, 1),
            "load_s": round(self.load_s, 1),
            "cache_hit": self.cache_hit,
            "cached_tokens_est": self.cached_tokens_est,
        }


class OpenAICompatBackend:
    name = "openai_compatible"
    local = False

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 600,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def _body(
        self,
        messages: list[dict],
        schema: dict | None,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "strict": True, "schema": schema},
            }
        return body

    def prepare(self) -> float:
        """调用前的准备（本地后端在此加载模型），返回耗时。"""
        return 0.0

    def chat(
        self,
        messages: list[dict],
        schema: dict | None = None,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        load_s = self.prepare()
        t = time.monotonic()
        res = ChatResult(text="", load_s=load_s)
        parts: list[str] = []
        thinking: list[str] = []
        url = f"{self.base_url}/v1/chat/completions"
        calls: dict[int, dict] = {}
        body = self._body(messages, schema, max_tokens, tools)
        with self.http.stream("POST", url, json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                d = json.loads(line[5:])
                for ch in d.get("choices") or []:
                    if not res.ttft_s:
                        res.ttft_s = time.monotonic() - t
                    delta = ch.get("delta") or {}
                    parts.append(delta.get("content") or "")
                    thinking.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
                    for tc in delta.get("tool_calls") or []:  # 流式工具调用：按 index 拼接
                        c = calls.setdefault(
                            tc.get("index", 0), {"id": None, "name": "", "arguments": ""}
                        )
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["arguments"] += fn.get("arguments") or ""
                if d.get("usage"):
                    res.prompt_tokens = d["usage"].get("prompt_tokens", 0)
                    res.completion_tokens = d["usage"].get("completion_tokens", 0)
        res.text = "".join(parts)
        res.reasoning = "".join(thinking)
        res.tool_calls = [{**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
        res.total_s = time.monotonic() - t
        return res

    def prewarm(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        """用当前前缀发一次 1 token 请求，把前缀写进缓存（tools 也属于前缀，必须一致）。"""
        return self.chat(messages, None, max_tokens=1, tools=tools)


class LMStudioBackend(OpenAICompatBackend):
    name = "lmstudio"
    local = True

    def __init__(
        self,
        base_url: str,
        model: str,
        context: int,
        ttl: Callable[[], int | None],
        allowed: Callable[[], bool] = lambda: True,
    ):
        super().__init__(base_url, model)
        self.context = context
        self.ttl = ttl  # 由驻留模式与会话活跃度决定
        self.allowed = allowed
        self.load_guard: Callable[[], str | None] | None = None  # 返回拒绝加载的原因

    def prepare(self) -> float:
        if not self.allowed():
            from .planner import PlannerUnavailable

            raise PlannerUnavailable("当前是纯命令模式，不唤醒 LLM；用 /mode on_demand 切回")
        if llm.loaded(self.model):
            return 0.0
        why = self.load_guard() if self.load_guard else None
        if why:  # 加载前把关：独占渲染在跑时不加载（任何进程、任何路径都经过这里）
            from .planner import PlannerUnavailable

            raise PlannerUnavailable(f"not loading the LLM now: {why}")
        t = time.monotonic()
        llm.load(self.model, self.context, self.ttl())
        return time.monotonic() - t

    def is_loaded(self) -> bool:
        return llm.loaded(self.model) is not None


class FuncBackend:
    """测试用：fn(messages, schema) → 文本。"""

    name = "func"
    local = False

    def __init__(self, fn: Callable[[list[dict], dict | None], str]):
        self.fn = fn
        self.calls: list[list[dict]] = []

    def chat(self, messages, schema=None, max_tokens=4096, tools=None) -> ChatResult:
        self.calls.append(messages)
        out = self.fn(messages, schema) if tools is None else self.fn(messages, tools)
        if isinstance(out, dict):  # 测试用：{"text": ..., "tool_calls": [...]}
            return ChatResult(
                text=out.get("text", ""),
                tool_calls=out.get("tool_calls", []),
                prompt_tokens=sum(len(m.get("content") or "") for m in messages) // 2,
            )
        return ChatResult(
            text=out,
            prompt_tokens=sum(len(m["content"]) for m in messages) // 2,
        )

    def prewarm(self, messages, tools=None) -> ChatResult:
        self.calls.append(messages)
        return ChatResult(text="")
