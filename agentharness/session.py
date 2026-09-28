"""Durable run/session state.

The working tree is already the durable source of file truth. This module persists the
agent state needed to continue the same run: task, plan, context/messages, counters,
active skills, verification summaries and a compact deterministic state digest.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


SESSION_VERSION = 1


def _atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, work_dir: Path, source_project: Path | str | None = None):
        self.work_dir = Path(work_dir).resolve()
        self.evidence_dir = self.work_dir / "evidence"
        self.session_path = self.evidence_dir / "session.json"
        self.messages_path = self.evidence_dir / "messages.json"
        self.digest_path = self.evidence_dir / "context-digest.json"
        self.journal_path = self.evidence_dir / "journal.json"
        self.source_project = str(Path(source_project).resolve()) if source_project else ""

    def exists(self) -> bool:
        return self.session_path.is_file()

    def load(self) -> dict:
        if not self.exists():
            raise FileNotFoundError(f"No resumable session at {self.session_path}")
        data = json.loads(self.session_path.read_text(encoding="utf-8"))
        if data.get("version") != SESSION_VERSION:
            raise ValueError(
                f"Unsupported session version {data.get('version')!r}; expected {SESSION_VERSION}"
            )
        return data

    def messages(self) -> list[dict]:
        if not self.messages_path.is_file():
            return []
        data = json.loads(self.messages_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def journal(self) -> list[dict]:
        if not self.journal_path.is_file():
            return []
        data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def save(self, state: dict, *, messages: list[dict], digest: dict,
             journal: list[dict]) -> None:
        previous = {}
        if self.session_path.is_file():
            try:
                previous = json.loads(self.session_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
        now = round(time.time(), 3)
        payload = {
            "version": SESSION_VERSION,
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            "work_dir": str(self.work_dir),
            "source_project": state.get("source_project") or previous.get("source_project")
                              or self.source_project,
            "resume_count": int(previous.get("resume_count", 0)),
            **state,
        }
        _atomic_json(self.session_path, payload)
        _atomic_json(self.messages_path, messages)
        _atomic_json(self.digest_path, digest)
        _atomic_json(self.journal_path, journal)

    def mark_resume(self) -> None:
        state = self.load()
        state["resume_count"] = int(state.get("resume_count", 0)) + 1
        state["updated_at"] = round(time.time(), 3)
        _atomic_json(self.session_path, state)
