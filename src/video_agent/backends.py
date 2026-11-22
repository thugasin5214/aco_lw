"""Planner backends (S9): session, memory and cache logic are independent of the concrete model service.

- `OpenAICompatBackend`: any OpenAI-compatible /v1/chat/completions (cloud or local).
- `LMStudioBackend`: adds local model load / TTL / prewarm management on top (G0: load ~15 s; prefix cache measured to work).
- `FuncBackend`: for tests, wraps a function.
Stage 6's Anthropic / agent CLI backends implement the same interface.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from . import llm

# the Messages API requires max_tokens; used when no cap is configured (llm_max_tokens = 0)
ANTHROPIC_MAX_TOKENS = 32000


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict] = field(default_factory=list)  # [{id, name, arguments(str)}]
    # the model's thinking: replayed within the same run so history matches generation verbatim
    # (cache)
    reasoning: str = ""
    raw_content: list | None = (
        # raw content blocks of online backends (e.g. Claude's thinking blocks), sent back unchanged
        # on replay
        None
    )
    # cache-hit tokens reported by the server (used instead of the estimate when present)
    cached_tokens: int | None = None
    backend: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_s: float = 0.0
    total_s: float = 0.0
    load_s: float = 0.0
    finish_reason: str = ""  # "length" = stopped at the output cap

    @property
    def cached_tokens_est(self) -> int:
        if self.cached_tokens is not None:
            return self.cached_tokens
        """Estimate reused prefix tokens: total minus what cold computation (G0 measured ~360 tokens/s) could do within the time to first token."""
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
            "via": self.backend,
            "finish_reason": self.finish_reason,
            "text_chars": len(self.text),
            "reasoning_chars": len(self.reasoning),
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
        max_tokens: int | None,
        tools: list[dict] | None = None,
    ) -> dict:
        # only the local template understands reasoning_content
        keep_reasoning = self.name == "lmstudio"
        messages = [
            {
                k: v
                for k, v in m.items()
                if not k.startswith("_") and (keep_reasoning or k != "reasoning_content")
            }
            for m in messages
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # no cap (None / 0): the model may write until its context is full — a thinking model
        # spends thousands of tokens before the answer. LM Studio takes -1 for that; other
        # servers get no field and use their own maximum.
        if max_tokens:
            body["max_tokens"] = max_tokens
        elif self.name == "lmstudio":
            body["max_tokens"] = -1
        # official OpenAI API: reasoning models only accept max_completion_tokens and the default
        # temperature
        if self.name == "openai":
            body.pop("temperature")
            if "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
        if tools:
            body["tools"] = tools
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "strict": True, "schema": schema},
            }
        return body

    def prepare(self) -> float:
        """Preparation before a call (the local backend loads the model here); returns the time spent."""
        return 0.0

    def chat(
        self,
        messages: list[dict],
        schema: dict | None = None,
        max_tokens: int | None = None,
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
            # include the server's reason (LM Studio says exactly what is wrong)
            if r.status_code >= 400:
                r.read()
                raise httpx.HTTPStatusError(
                    f"{r.status_code} from {url}: {_error_text(r)}", request=r.request, response=r
                )
            for line in r.iter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                d = json.loads(line[5:])
                for ch in d.get("choices") or []:
                    if not res.ttft_s:
                        res.ttft_s = time.monotonic() - t
                    res.finish_reason = ch.get("finish_reason") or res.finish_reason
                    delta = ch.get("delta") or {}
                    parts.append(delta.get("content") or "")
                    thinking.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
                    for tc in delta.get("tool_calls") or []:  # streamed tool calls: join by index
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
                    details = d["usage"].get("prompt_tokens_details") or {}
                    # real cache hits reported by the server
                    if details.get("cached_tokens") is not None:
                        res.cached_tokens = details["cached_tokens"]
        res.text = "".join(parts)
        res.reasoning = "".join(thinking)
        res.backend = self.name
        res.tool_calls = [{**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
        res.total_s = time.monotonic() - t
        return res

    def prewarm(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        """Send a 1-token request with the current prefix to write it into the cache (tools are part of the prefix and must match)."""
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
        cfg=None,
    ):
        super().__init__(base_url, model)
        # with a config: model / context / GPU / parallel follow the settings window live
        self.cfg = cfg
        self._context = context
        self.ttl = ttl  # decided by the residency mode and session activity
        self.allowed = allowed
        # returns the reason to refuse loading
        self.load_guard: Callable[[], str | None] | None = None

    @property
    def model(self) -> str:
        return self.cfg.llm_model if self.cfg else self._model

    @model.setter
    def model(self, v: str) -> None:
        self._model = v

    @property
    def context(self) -> int:
        return self.cfg.llm_context if self.cfg else self._context

    def prepare(self) -> float:
        if not self.allowed():
            from .planner import PlannerUnavailable

            raise PlannerUnavailable("当前是纯命令模式，不唤醒 LLM；用 /mode on_demand 切回")
        if llm.loaded(self.model):
            return 0.0
        why = self.load_guard() if self.load_guard else None
        # gate before loading: no load while an exclusive render runs (every process and path passes
        # here)
        if why:
            from .planner import PlannerUnavailable

            raise PlannerUnavailable(f"not loading the LLM now: {why}")
        t = time.monotonic()
        c = self.cfg
        llm.load(
            self.model,
            self.context,
            self.ttl(),
            getattr(c, "llm_gpu", "") if c else "",
            getattr(c, "llm_parallel", 0) if c else 0,
        )
        return time.monotonic() - t

    def is_loaded(self) -> bool:
        return llm.loaded(self.model) is not None

    def _body(self, messages, schema, max_tokens, tools=None):
        # switched to a model without vision: images become a text note
        if llm.supports_vision(self.model) is False:
            messages = strip_images(messages, self.model)
        return super()._body(messages, schema, max_tokens, tools)


def strip_images(messages: list[dict], model: str) -> list[dict]:
    note = f"[image omitted: the current local model {model} cannot see images]"
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(p.get("type") == "image_url" for p in c):
            n = sum(p.get("type") == "image_url" for p in c)
            text = "\n".join(p.get("text", "") for p in c if p.get("type") == "text").strip()
            m = {**m, "content": (text + "\n" if text else "") + note + (f" ×{n}" if n > 1 else "")}
        out.append(m)
    return out


def _error_text(r: httpx.Response) -> str:
    try:
        d = r.json()
        e = d.get("error", d)
        return str(e.get("message", e) if isinstance(e, dict) else e)[:500]
    except ValueError:
        return r.text[:500]


class FuncBackend:
    """For tests: fn(messages, schema) → text. Plays a local LLM by default (subject to the resource gate)."""

    name = "func"
    local = True

    def __init__(self, fn: Callable[[list[dict], dict | None], str]):
        self.fn = fn
        self.calls: list[list[dict]] = []

    def chat(self, messages, schema=None, max_tokens=None, tools=None) -> ChatResult:
        self.calls.append(messages)
        out = self.fn(messages, schema) if tools is None else self.fn(messages, tools)
        if isinstance(out, dict):  # for tests: {"text": ..., "tool_calls": [...]}
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


_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class AnthropicBackend:
    """Online Claude (Messages API + native tool calls).

    - Input stays the runtime's OpenAI-style messages; converted to the Messages API format here.
    - Turns Claude generated itself are replayed as their original content blocks (including thinking), so history only appends.
    - Caching: a breakpoint on the stable prefix (tools + system), plus top-level automatic caching covering the latest message.
    - When the safety classifier refuses, the server falls back to the recommended model by default (fallbacks="default").
    """

    name = "anthropic"
    local = False
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        api_key: str | None = None,
        fallbacks: bool = True,
        client=None,
    ):
        import anthropic

        self.model, self.effort, self.fallbacks = model, effort, fallbacks
        # without a key, the SDK resolves credentials from environment variables / `ant auth login`
        self.client = client or (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )

    @staticmethod
    def _id(x: str) -> str:
        return _ID_RE.sub("_", x or "call")

    @staticmethod
    def _user_content(content):
        """OpenAI-style content parts (text / image_url data URL) → Claude content blocks."""
        if not isinstance(content, list):
            return content
        out = []
        for p in content:
            if p.get("type") == "image_url":
                url = p["image_url"]["url"]
                head, _, data = url.partition(",")
                media = head.removeprefix("data:").split(";")[0] or "image/jpeg"
                out.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data},
                    }
                )
            else:
                out.append({"type": "text", "text": p.get("text", "")})
        return out

    @classmethod
    def convert(cls, messages: list[dict], tools: list[dict] | None):
        system_parts, out, results = [], [], []

        def flush():  # all tool results of one turn go into a single user message
            if results:
                out.append({"role": "user", "content": list(results)})
                results.clear()

        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append(m["content"])
                continue
            if role == "tool":
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": cls._id(m["tool_call_id"]),
                        "content": m.get("content") or "",
                    }
                )
                continue
            flush()
            if role == "assistant":
                # a turn this backend generated: replay it unchanged (including thinking blocks)
                if m.get("_anthropic"):
                    out.append({"role": "assistant", "content": m["_anthropic"]})
                    continue
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for c in m.get("tool_calls") or []:
                    fn = c["function"]
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": cls._id(c["id"]),
                            "name": fn["name"],
                            "input": json.loads(fn.get("arguments") or "{}"),
                        }
                    )
                out.append(
                    {
                        "role": "assistant",
                        "content": blocks or [{"type": "text", "text": "(no content)"}],
                    }
                )
            else:
                out.append({"role": "user", "content": cls._user_content(m["content"])})
        flush()
        atools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools or []
        ]
        return "\n\n".join(system_parts), out, atools

    def prepare(self) -> float:
        return 0.0

    def chat(
        self,
        messages: list[dict],
        schema: dict | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        max_tokens = max_tokens or ANTHROPIC_MAX_TOKENS  # the API requires a cap
        system, msgs, atools = self.convert(messages, tools)
        if schema:  # JSON output in simple mode: constrain with instructions, the caller validates
            system += (
                "\n\nReply with a single JSON object matching this schema, nothing else:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": msgs,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if atools:
            kwargs["tools"] = atools
        betas = []
        if self.fallbacks:
            betas.append(self.FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        t = time.monotonic()
        resp = self.client.beta.messages.create(betas=betas, **kwargs)
        total = time.monotonic() - t
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            text = f"(declined by Claude's safety classifier: {cat or 'unspecified'})"
        calls = [
            {"id": b.id, "name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}
            for b in resp.content
            if b.type == "tool_use"
        ]
        u = resp.usage
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        made = getattr(u, "cache_creation_input_tokens", 0) or 0
        return ChatResult(
            text=text,
            tool_calls=calls,
            prompt_tokens=(u.input_tokens or 0) + read + made,
            completion_tokens=u.output_tokens or 0,
            ttft_s=total,
            total_s=total,
            raw_content=[b.model_dump(exclude_none=True) for b in resp.content],
            cached_tokens=read,
            backend=f"anthropic:{resp.model}",
        )

    def prewarm(self, messages: list[dict], tools: list[dict] | None = None) -> None:
        return None  # the online cache is written automatically by real requests; no prewarm needed
