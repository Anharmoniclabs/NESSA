"""Independent advisory reviewer model.

The reviewer never executes tools and never decides completion. It critiques the current
patch and verification evidence; the deterministic controller remains authoritative.
"""
from __future__ import annotations

from .llm import ModelError


REVIEW_PROMPT = """You are an independent code-review model. Review the proposed patch against the
task and evidence. Look for incorrect behavior, missing edge cases, accidental scope expansion,
weak or missing tests, and architectural violations. Be concise and concrete. Do not claim the
work is correct merely because tests passed. Do not use tools. Return review notes only."""


class Reviewer:
    def __init__(self, client):
        self.client = client

    def review(self, task: str, patch: str, checks: str = "") -> str:
        if not patch.strip():
            return "No patch to review."
        messages = [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": f"Task:\n{task}\n\nVerification evidence:\n{checks or '(none)'}\n\nPatch:\n{patch[:24000]}"},
        ]
        try:
            reply = self.client.chat(messages, None, set())
        except ModelError as exc:
            return f"Reviewer unavailable: {exc}"
        text = (reply.content or "").strip()
        return text or "Reviewer returned no notes."
