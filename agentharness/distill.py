"""Distillation: record cloud (teacher) replies locally so small local models can learn from them.

Every reply a cloud model produces through FallbackClient is appended, with the exact context and
tool schemas it saw, to ~/.agentharness/distill/turns.jsonl. When a run ends, its status is
appended to outcomes.jsonl. `export` keeps only turns from runs that ended well, scrubs secrets,
de-duplicates, and writes chat-format JSONL ({"messages", "tools"}) for SFT/LoRA training
(see nessa-coder-lora/nessa_distill_lora.ipynb).

Nothing here leaves the machine. Local-model replies are never recorded: a student must not learn
from itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

DEFAULT_DIR = Path.home() / ".agentharness" / "distill"
GOOD = ("verified", "completed", "answered")  # no_change is too weak a signal to imitate
SECRETS = [
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "hf_<redacted>"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{20,}"), r"\1-<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "gh_<redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA<redacted>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted private key>"),
    (re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*)['\"]?[^\s'\"]{6,}"),
     r"\1<redacted>"),
]


def scrub(value):
    if isinstance(value, str):
        for pattern, replacement in SECRETS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


class Recorder:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or DEFAULT_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = f"adhoc-{int(time.time())}"

    def begin(self, run_id: str):
        self.run_id = run_id

    def _append(self, name: str, row: dict):
        with (self.directory / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def turn(self, teacher: str, messages: list, tools: list | None, reply) -> None:
        assistant = {"role": "assistant", "content": reply.content or ""}
        if reply.native and reply.raw_tool_calls:
            assistant["tool_calls"] = reply.raw_tool_calls
        self._append("turns.jsonl", dict(t=time.time(), run=self.run_id, teacher=teacher,
                                         messages=scrub(messages), tools=tools or [],
                                         reply=scrub(assistant)))

    def outcome(self, status: str) -> None:
        self._append("outcomes.jsonl", dict(t=time.time(), run=self.run_id, status=status))


def _rows(path: Path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def stats(directory: Path | None = None) -> dict:
    directory = Path(directory or DEFAULT_DIR)
    outcomes = {r["run"]: r["status"] for r in _rows(directory / "outcomes.jsonl")}
    teachers, statuses, turns = {}, {}, 0
    for row in _rows(directory / "turns.jsonl"):
        turns += 1
        teachers[row["teacher"]] = teachers.get(row["teacher"], 0) + 1
        status = outcomes.get(row["run"], "unfinished")
        statuses[status] = statuses.get(status, 0) + 1
    return dict(turns=turns, runs=len(outcomes), by_teacher=teachers, by_run_status=statuses)


def export(out: Path, directory: Path | None = None, good=GOOD, max_chars: int = 24000,
           include_unfinished: bool = False) -> int:
    """Write {"messages": [...], "tools": [...]} rows; returns how many were written."""
    directory = Path(directory or DEFAULT_DIR)
    outcomes = {r["run"]: r["status"] for r in _rows(directory / "outcomes.jsonl")}
    seen, written = set(), 0
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with Path(out).open("w", encoding="utf-8") as f:
        for row in _rows(directory / "turns.jsonl"):
            status = outcomes.get(row["run"])
            if status not in good and not (include_unfinished and status is None):
                continue
            reply = row["reply"]
            if not reply.get("content", "").strip() and not reply.get("tool_calls"):
                continue
            messages = list(row["messages"])
            # Drop the oldest middle exchanges until the example fits; keep system + latest turns.
            while len(json.dumps(messages, ensure_ascii=False)) > max_chars and len(messages) > 3:
                del messages[1]
            while len(messages) > 1 and messages[1].get("role") == "tool":
                del messages[1]  # never start history with an orphaned tool result
            example = dict(messages=messages + [reply], tools=row.get("tools") or [])
            key = hashlib.sha256(json.dumps(example, sort_keys=True).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            f.write(json.dumps(example, ensure_ascii=False) + "\n")
            written += 1
    return written
