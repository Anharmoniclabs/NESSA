"""Optional action-policy hook (e.g. a NEUDO-style ranker).

The controller builds the set of permitted tools; a policy may only reorder or narrow
that set. It can never add a tool, approve a plan, or declare a task complete.
    shadow: preferences are logged, the model still sees every permitted tool
    active: the model only sees the policy's top-k permitted tools (plus `finish`)
"""
from __future__ import annotations

from typing import Protocol


class ActionPolicy(Protocol):
    mode: str   # "shadow" | "active"
    top_k: int

    def rank(self, state: dict, permitted: list[str]) -> list[str]: ...


def apply_policy(policy: ActionPolicy | None, state: dict, permitted: list[str]) -> tuple[list[str], list[str]]:
    """Returns (tools to offer, the policy's ranking for the log)."""
    if policy is None:
        return permitted, []
    ranking = [t for t in policy.rank(state, list(permitted)) if t in permitted]  # cannot invent tools
    if policy.mode != "active" or not ranking:
        return permitted, ranking
    offered = ranking[:max(1, policy.top_k)]
    for keep in ("finish", "propose_plan"):
        if keep in permitted and keep not in offered:
            offered.append(keep)
    return offered, ranking
