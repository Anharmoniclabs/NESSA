"""Per-call permission modes, modelled on enterprise CLI agents.

    plan          read-only: anything that changes files, runs commands or commits is denied
    default       reads run freely; edits, commands, dev processes, commits and MCP calls ask
    accept_edits  reads and file edits run freely; commands, commits and MCP calls ask
    bypass        everything runs without asking

Registered checks (run_check) are trusted project commands and never ask. An "always" answer
is remembered per tool for the rest of the session. The legacy private-workspace workflow
uses no permission mode (None): its plan approval already covers the whole run.
"""
from __future__ import annotations

from typing import Callable

MODES = ('plan', 'default', 'accept_edits', 'bypass')
# Tool kinds that never mutate anything.
FREE_KINDS = frozenset({'read', 'control'})
# Calls a registered check makes are project-declared commands, not model-invented ones.
TRUSTED_TOOLS = frozenset({'run_check', 'agent'})

# asker(tool_name, args) -> (allowed, feedback, always)
Asker = Callable[[str, dict], tuple[bool, str, bool]]


def deny_all(name: str, args: dict) -> tuple[bool, str, bool]:
    return False, 'No interactive approver is attached; use accept_edits or bypass for unattended runs.', False


class Permissions:
    def __init__(self, mode: str, asker: Asker | None = None):
        if mode not in MODES:
            raise ValueError(f'Unknown permission mode {mode!r}; choose one of {", ".join(MODES)}')
        self.mode = mode
        self.asker = asker or deny_all
        self.always: set[str] = set()

    def check(self, name: str, kind: str, args: dict) -> tuple[bool, str]:
        """Return (allowed, reason). The reason is shown to the model when denied."""
        if kind in FREE_KINDS or name in TRUSTED_TOOLS:
            return True, ''
        if self.mode == 'bypass':
            return True, ''
        if self.mode == 'plan':
            return False, f'Plan mode is read-only: {name} is not allowed. Present the plan instead.'
        if self.mode == 'accept_edits' and kind == 'edit':
            return True, ''
        if name in self.always:
            return True, ''
        allowed, feedback, always = self.asker(name, args)
        if allowed and always:
            self.always.add(name)
        if allowed:
            return True, ''
        return False, f'The user denied {name}.' + (f' Feedback: {feedback}' if feedback else '')
