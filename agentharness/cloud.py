"""Opt-in cloud models through the Hugging Face Inference Providers router, with local fallback.

Cloud use sends prompts, project files and tool results to Hugging Face and the provider that
serves the model. It is never enabled implicitly: the CLI requires --cloud.

FallbackClient tries each client in order and moves on when one is unavailable (network error,
exhausted credits, rate limit, provider outage). A client that fails is skipped for a cooldown;
one whose credits are exhausted or whose token is rejected is skipped for the whole session.
The last client is normally the local model, so work continues offline.

The token is read from HF_TOKEN, or from a file (default ~/Desktop/HF). It is only ever sent
in the Authorization header to the router and is never written to logs, sessions or transcripts.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .llm import ChatClient, ContextOverflow, ModelError, PartialResponse

HF_ROUTER = "https://router.huggingface.co/v1"
DEFAULT_TOKEN_FILE = Path.home() / "Desktop" / "HF"
# Chosen 2026-10-06 by a tool-calling coding probe: all three fixed the bug correctly with one
# call; GLM-5.3 was fastest per useful token (2.2 s, 113 output tokens). Override with --cloud-models.
DEFAULT_MODELS = ("zai-org/GLM-5.3", "moonshotai/Kimi-K3", "deepseek-ai/DeepSeek-V4-Pro-0813")
COOLDOWN = 120.0
FATAL = ("depleted", "credits", "payment", "http 401", "http 402", "http 403", "not supported by any provider")


def load_token(path: str | os.PathLike | None = None) -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        return token
    file = Path(path).expanduser() if path else DEFAULT_TOKEN_FILE
    try:
        token = file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"No Hugging Face token: set HF_TOKEN or create {file}") from exc
    if not token.startswith("hf_"):
        raise ValueError(f"{file} does not contain a Hugging Face token (expected hf_...)")
    return token


def cloud_clients(token: str, models=DEFAULT_MODELS, *, max_tokens: int = 4096,
                  temperature: float = 0.0, timeout: float = 180) -> list[ChatClient]:
    # retries=1: a failing provider should hand over to the next model immediately.
    return [ChatClient(HF_ROUTER, model, max_tokens=max_tokens, temperature=temperature,
                       timeout=timeout, retries=1, allow_remote=True, api_key=token)
            for model in models]


class FallbackClient:
    """Duck-types ChatClient; `model`/`base_url` describe the client that served the last call."""

    def __init__(self, clients: list, on_switch=None, clock=time.monotonic):
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        self.clients = list(clients)
        self.active = self.clients[0]
        self.on_switch = on_switch
        self.clock = clock
        self.skip_until: dict[int, float] = {}
        self.failures: list[str] = []

    def __getattr__(self, name):  # model, base_url, max_tokens, on_delta, ...
        return getattr(self.__dict__["active"], name)

    def __setattr__(self, name, value):
        if name == "on_delta":  # the desktop streams through whichever client answers
            for client in self.clients:
                client.on_delta = value
        object.__setattr__(self, name, value)

    def available(self) -> list:
        now = self.clock()
        return [c for i, c in enumerate(self.clients) if self.skip_until.get(i, 0) <= now]

    def chat(self, messages, tools=None, tool_names=None):
        errors = []
        candidates = self.available() or self.clients[-1:]
        for client in candidates:
            try:
                reply = client.chat(messages, tools, tool_names)
            except (ContextOverflow, PartialResponse, KeyboardInterrupt):
                raise  # the controller compacts or saves visible text; another model would repeat it
            except ModelError as exc:
                text = str(exc)
                fatal = any(word in text.lower() for word in FATAL)
                self.skip_until[self.clients.index(client)] = float("inf") if fatal else self.clock() + COOLDOWN
                errors.append(f"{client.model}: {text[:200]}")
                self.failures.append(errors[-1])
                continue
            if client is not self.active:
                previous, self.active = self.active, client
                if self.on_switch:
                    self.on_switch(previous.model, client.model, errors)
            return reply
        raise ModelError("All models unavailable: " + " | ".join(errors))
