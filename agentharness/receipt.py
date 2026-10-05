"""Verification receipts: bind check evidence to the exact workspace and checks that produced it.

A receipt says "these checks, with these definitions, gave these statuses against this
workspace content". It does not say the checks are sufficient; it only prevents evidence
from one version being reused for another. Hashes detect accidental change in a
single-writer run; they are not an authenticated security boundary.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field

FAILING = ('failed', 'timeout', 'setup_error', 'error')


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode('utf-8', 'surrogatepass')).hexdigest()


def workspace_fingerprint(ws) -> str:
    """Hash of everything that differs from the baseline (paths and exact bytes)."""
    return _sha(ws.patch())


def checks_fingerprint(runner, names) -> str:
    """Hash of the registered definition of each named check (command text, or name for callables)."""
    parts = []
    for name in sorted(names):
        definition = runner.checks.get(name)
        parts.append([name, definition if isinstance(definition, str) else ('callable' if definition else None)])
    return _sha(json.dumps(parts, sort_keys=True))


@dataclass
class VerificationReceipt:
    workspace_sha: str
    checks_sha: str
    statuses: dict = field(default_factory=dict)
    failure_signature: str = ''
    created_at: float = field(default_factory=time.time)

    @property
    def failing(self) -> list:
        return sorted(n for n, s in self.statuses.items() if s in FAILING)

    def to_dict(self) -> dict:
        return asdict(self)


def make_receipt(ws, runner, results: dict) -> VerificationReceipt:
    """`results` maps check name -> CheckResult. Failure output is hashed so identical failures compare equal."""
    failing = {n: r for n, r in results.items() if r.status in FAILING}
    signature = _sha(json.dumps({n: [r.status, r.exit_code, r.counts, r.output] for n, r in sorted(failing.items())},
                                sort_keys=True, default=str)) if failing else ''
    return VerificationReceipt(workspace_fingerprint(ws), checks_fingerprint(runner, results),
                               {n: r.status for n, r in results.items()}, signature)


def matches_current(receipt: VerificationReceipt | None, ws, runner) -> bool:
    return bool(receipt and receipt.workspace_sha == workspace_fingerprint(ws)
                and receipt.checks_sha == checks_fingerprint(runner, receipt.statuses))


def repeats(previous: VerificationReceipt | None, current: VerificationReceipt) -> bool:
    """True when a failed finish is retried with no new evidence: same files, same checks, same failure."""
    return bool(previous and current.failure_signature and previous.failure_signature == current.failure_signature
                and previous.workspace_sha == current.workspace_sha
                and previous.checks_sha == current.checks_sha)
