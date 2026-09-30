"""Opt-in native Ollama chat transport; structured thinking is never retained.

The server owns its model's chat template. Only a completed final channel can
produce tool/source proposals. This client has no cloud or /v1 fallback.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .llm import LOCAL_HOSTS, ModelError, Reply, ToolCall, parse_text_tool_calls

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_NATIVE_TOKENS = 1024
_THINK_TAG = re.compile(r"</?think(?:ing)?\s*>", re.I)


class OllamaDeadlineExceeded(ModelError):
    """The native request exhausted its client-side elapsed deadline."""


@dataclass
class OllamaReply(Reply):
    completion_tokens: int = 0  # server eval_count, includes thinking + final
    cached_prompt_tokens: int = 0
    transport_metadata: dict = field(default_factory=dict)


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, think: bool | str,
                 max_tokens: int = 768, timeout: float = 180,
                 temperature: float = 0.0, num_ctx: int = 8192,
                 num_thread: int = 2, seed: int = 42):
        url = urllib.parse.urlsplit(base_url)
        if (url.scheme != "http" or url.hostname not in LOCAL_HOSTS or url.username or
                url.password or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("Native Ollama requires a loopback HTTP origin, without /v1, credentials or a query.")
        if type(think) is not bool and (not isinstance(think, str) or not think or len(think) > 64):
            raise ValueError("Native Ollama requires an explicit boolean or named thinking mode.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A model name is required.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Native Ollama timeout must be finite and positive.")
        if not math.isfinite(temperature) or temperature < 0:
            raise ValueError("Temperature must be finite and nonnegative.")
        if type(num_ctx) is not int or not 512 <= num_ctx <= 131072:
            raise ValueError("num_ctx must be 512..131072.")
        if type(num_thread) is not int or not 1 <= num_thread <= 256:
            raise ValueError("num_thread must be 1..256.")
        if type(seed) is not int or not 0 <= seed < 2**31:
            raise ValueError("seed must be a nonnegative 31-bit integer.")
        self.base_url, self.model, self.think = base_url.rstrip("/"), model, think
        self.max_tokens, self.timeout, self.temperature = max_tokens, timeout, temperature
        self.options = {"num_ctx": num_ctx, "num_thread": num_thread, "seed": seed}
        self.metadata = None
        self._host, self._port = url.hostname, url.port or 80
        self._token_limit()

    def _token_limit(self):
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= MAX_NATIVE_TOKENS:
            raise ValueError(f"Native Ollama max_tokens must be 1..{MAX_NATIVE_TOKENS}, shared by thinking and final output.")
        return self.max_tokens

    def _request(self, route, body, timeout):
        # A socket inactivity timeout alone is not an elapsed bound: a slow peer
        # can keep it alive indefinitely. A watchdog shuts down the captured
        # socket, including after HTTPConnection transfers it to the response.
        started = time.monotonic()
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        timer = None
        response = None
        expired = threading.Event()
        try:
            connection.connect()
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise OllamaDeadlineExceeded("Native Ollama request deadline exceeded; no output accepted.")
            sock = connection.sock
            def cancel():
                expired.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining, cancel)
            timer.daemon = True
            timer.start()
            connection.request("POST", route, body=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                # No redirects, body echo, implicit proxy, credentials or retry.
                raise ModelError(f"Native Ollama HTTP {response.status}; request failed without fallback.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if expired.is_set() or time.monotonic() - started > timeout:
                raise OllamaDeadlineExceeded("Native Ollama request deadline exceeded; no output accepted.")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ModelError("Native Ollama response exceeds the bounded response size.")
            data = json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if expired.is_set() or time.monotonic() - started >= timeout:
                raise OllamaDeadlineExceeded("Native Ollama request deadline exceeded; no output accepted.") from None
            # A malformed status line or server body may contain private text.
            raise ModelError(f"Native Ollama request failed ({type(exc).__name__}); no output accepted.") from None
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            connection.close()
        if not isinstance(data, dict) or data.get("error"):
            raise ModelError("Native Ollama returned an error or invalid response object.")
        if data.get("remote_host") or data.get("remote_model"):
            raise ModelError("Native Ollama remote-backed models are unsupported; local inference is required.")
        return data

    def discover(self):
        data = self._request("/api/show", {"model": self.model}, min(10, self.timeout))
        info = data.get("thinking")
        values = info.get("values") if isinstance(info, dict) else None
        metadata_source = "thinking.values"
        capabilities = data.get("capabilities")
        if ("thinking" not in data and self.think is True and isinstance(capabilities, list)
                and all(isinstance(v, str) for v in capabilities) and "thinking" in capabilities):
            # Legacy model packages can advertise thinking capability without
            # a values list. Only explicit true is justified by that capability;
            # false and named levels remain unsupported without exact metadata.
            values = [True]
            metadata_source = "legacy thinking capability (true only)"
        if (not isinstance(values, list) or not values or
                any(type(v) is not bool and (not isinstance(v, str) or not v or len(v) > 64) for v in values)):
            raise ModelError("Native Ollama model lacks supported thinking-mode metadata; explicit mode cannot be verified.")
        if not any(type(v) is type(self.think) and v == self.think for v in values):
            raise ModelError("Requested thinking mode is not advertised by this Ollama model; no fallback used.")
        self.metadata = {"transport": "ollama-native", "think": self.think,
            "supported_think_values": values, "thinking_metadata_source": metadata_source, "thinking_storage": "omitted; character count only",
            "generated_token_budget": "shared thinking and final", "options": dict(self.options)}
        return dict(self.metadata)

    @staticmethod
    def _count(data, key):
        value = data.get(key, 0)
        if type(value) is not int or value < 0:
            raise ModelError("Native Ollama returned invalid usage or timing metadata.")
        return value

    def chat(self, messages, tools=None, tool_names=None):
        tokens = self._token_limit()
        if self.metadata is None:
            self.discover()
        # Three-stage uses fresh system/user packets, never private reasoning history.
        if any(not isinstance(m, dict) or m.get("role") not in ("system", "user") or
               not isinstance(m.get("content"), str) for m in messages):
            raise ModelError("Native three-stage transport accepts fresh system/user text packets only.")
        body = {"model": self.model,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                "stream": False, "think": self.think, "truncate": False, "shift": False,
                "options": {**self.options, "temperature": self.temperature, "num_predict": tokens}}
        if tools:
            body["tools"] = tools
        data = self._request("/api/chat", body, self.timeout)
        if data.get("done") is not True or data.get("done_reason") not in ("stop", "length"):
            raise ModelError("Native Ollama did not return a supported completed response; no action accepted.")
        msg = data.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            raise ModelError("Native Ollama response lacks an assistant message.")
        content, thinking = msg.get("content", ""), msg.get("thinking", "")
        if content is None:
            content = ""
        if thinking is None:
            thinking = ""
        if not isinstance(content, str) or not isinstance(thinking, str):
            raise ModelError("Native Ollama returned invalid final/thinking channel types.")
        if _THINK_TAG.search(content):
            raise ModelError("Native Ollama final channel contains thinking delimiters; unsupported template output omitted.")
        metadata = {**self.metadata, "num_predict": tokens, "thinking_chars": len(thinking),
            "thinking_present": bool(thinking), "done_reason": data["done_reason"],
            "timing_ns": {n: self._count(data, n) for n in
                ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")}}
        prompt = self._count(data, "prompt_eval_count")
        completion = self._count(data, "eval_count")
        cached = self._count(data, "prompt_eval_cached_count")
        if completion > tokens:
            raise ModelError("Native Ollama exceeded its requested generated-token budget; no output accepted.")
        # Even a complete-looking JSON call at the limit cannot authorize an action.
        if data["done_reason"] == "length":
            return OllamaReply("", [], False, reasoning=None, prompt_tokens=prompt,
                finish_reason="length", completion_tokens=completion, cached_prompt_tokens=cached,
                transport_metadata=metadata)
        raw_calls = msg.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelError("Native Ollama tool calls must be a list.")
        calls, retained = [], []
        for i, call in enumerate(raw_calls):
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
                raise ModelError("Native Ollama returned an invalid function call.")
            args = fn.get("arguments")
            if not isinstance(args, dict):
                raise ModelError("Native Ollama function arguments must be an object.")
            safe = {"function": {"name": fn["name"], "arguments": args}}
            retained.append(safe)
            calls.append(ToolCall(f"ollama_{i}", fn["name"], args, json.dumps(args)))
        native = bool(calls)
        if not calls and content:
            calls = parse_text_tool_calls(content, tool_names)
        if not content.strip() and not calls:
            raise ModelError("Native Ollama returned no final answer or tool call; thinking alone is not a proposal.")
        return OllamaReply(content, calls, native, retained, reasoning=None, prompt_tokens=prompt,
            finish_reason="stop", completion_tokens=completion, cached_prompt_tokens=cached,
            transport_metadata=metadata)
