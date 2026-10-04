"""OpenAI-compatible chat client for local model servers (vLLM, Ollama, llama.cpp).

Local-only by default: there is no cloud fallback. Tool calls are normalised so the
controller sees the same shape whether the server returned native `tool_calls` or
the model wrote a JSON tool call in plain text (common with small models).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ModelError(RuntimeError):
    pass


class ContextOverflow(ModelError):
    """The server rejected the request, usually because the prompt is too long."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | None  # None when the model sent unparseable JSON
    raw: str = ""


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]
    native: bool                      # True: server-side tool_calls; False: parsed from text
    raw_tool_calls: list = field(default_factory=list)
    reasoning: str | None = None
    prompt_tokens: int = 0
    finish_reason: str | None = None


class ChatClient:
    def __init__(self, base_url: str, model: str, *, temperature: float = 0.0,
                 max_tokens: int = 4096, timeout: float = 900, retries: int = 3,
                 allow_remote: bool = False, api_key: str = "local", reasoning_effort: str | None = None):
        host = urllib.parse.urlparse(base_url).hostname
        if host not in LOCAL_HOSTS and not allow_remote:
            raise ValueError(f"Refusing non-local model server {host!r}; pass allow_remote=True to override.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        # Local traffic must not go through an HTTP proxy configured for the internet.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, route: str, body: dict | None = None, timeout: float | None = None) -> dict:
        req = urllib.request.Request(
            self.base_url + route, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        err: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._opener.open(req, timeout=timeout or self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as exc:
                detail = exc.read(4000).decode(errors="replace")
                if exc.code == 400:
                    if any(word in detail.lower() for word in ('context length', 'context window', 'too many tokens', 'context size')):
                        raise ContextOverflow(detail) from exc
                    raise ModelError(f'HTTP 400: {detail}') from exc
                if exc.code in (401, 403, 404):
                    raise ModelError(f"HTTP {exc.code}: {detail}") from exc
                err = ModelError(f"HTTP {exc.code}: {detail}")
            except (OSError, ValueError) as exc:
                err = exc
            time.sleep(2 * (attempt + 1))
        raise ModelError(f"Model server unavailable after {self.retries} attempts: {err}")

    def models(self) -> list[str]:
        return [m.get("id", "") for m in self._request("/models", timeout=10).get("data", [])]

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_names: set[str] | None = None) -> Reply:
        body = {"model": self.model, "messages": messages,
                "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = self._request("/chat/completions", body)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"No choices in response: {str(data)[:500]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
        if not isinstance(raw_calls, list) or len(raw_calls) > 64:
            raise ModelError('Invalid or oversized tool call envelope')
        calls = []
        normalized = []
        seen_ids = set()
        for i, c in enumerate(raw_calls):
            if not isinstance(c, dict) or not isinstance(c.get('function'), dict):
                raise ModelError('Malformed tool call')
            fn = c['function']
            if not isinstance(fn.get('name'), str):
                raise ModelError('Tool name must be a string')
            call_id = c.get('id') or f'call_{i}'
            if not isinstance(call_id, str) or call_id in seen_ids:
                raise ModelError('Duplicate or invalid tool call ID')
            seen_ids.add(call_id)
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                    args = args if isinstance(args, dict) else None
                except ValueError:
                    args = None
            calls.append(ToolCall(call_id, fn['name'], args, str(raw_args)))
            normalized.append({'id': call_id, 'type': 'function', 'function': {
                'name': fn['name'], 'arguments': raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})}})
        native = bool(calls)
        if not calls and content:
            calls = parse_text_tool_calls(content, tool_names)
        return Reply(content=content, tool_calls=calls, native=native, raw_tool_calls=normalized,
                     reasoning=msg.get("reasoning_content") or msg.get("reasoning"),
                     prompt_tokens=int((data.get("usage") or {}).get("prompt_tokens") or 0),
                     finish_reason=choices[0].get("finish_reason"))


_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)


def parse_text_tool_calls(text: str, tool_names: set[str] | None = None) -> list[ToolCall]:
    """Find JSON tool calls written as text: <tool_call>{...}</tool_call>, ```json {...}```,
    or a bare object with "name" and "arguments"/"parameters"."""
    candidates = _TAGGED.findall(text) or [text]
    decoder = json.JSONDecoder()
    calls: list[ToolCall] = []
    for chunk in candidates:
        pos = 0
        while (start := chunk.find("{", pos)) != -1:
            try:
                obj, end = decoder.raw_decode(chunk, start)
            except ValueError:
                pos = start + 1
                continue
            pos = end
            if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
                continue
            if tool_names is not None and obj["name"] not in tool_names:
                continue
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            calls.append(ToolCall(f"text_{len(calls)}", obj["name"],
                                  args if isinstance(args, dict) else None, json.dumps(obj)[:2000]))
    return calls
