"""agentharness: a small, local-first autonomous coding agent harness.

    inspect -> plan -> approve -> edit a private snapshot -> verify -> patch + evidence

The model proposes; the controller decides what is permitted and what counts as done.
Also includes document extraction (text/HTML/DOCX/PDF/OCR) with regex tabulation.
"""
from .agent import Agent, AgentConfig, RunResult
from .checks import CheckResult, CheckRunner, detect_checks
from .llm import ChatClient
from .workspace import Workspace

__all__ = ["Agent", "AgentConfig", "RunResult", "CheckResult", "CheckRunner", "detect_checks",
           "ChatClient", "Workspace"]
__version__ = "0.2.0"
