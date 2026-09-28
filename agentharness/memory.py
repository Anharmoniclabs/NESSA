"""Repository-scoped lessons: short, human-reviewed notes injected into future runs.

Only lessons you add (or approve) are stored; the agent never writes here on its own.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / ".agentharness" / "lessons.jsonl"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower())}


class LessonStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)

    def all(self, repo_key: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return [r for r in rows if repo_key is None or r["repo"] in (repo_key, "*")]

    def add(self, repo_key: str, text: str, source: str = "user") -> dict:
        row = {"t": time.time(), "repo": repo_key, "text": text.strip(), "source": source}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def relevant(self, repo_key: str, task: str, k: int = 5) -> list[str]:
        """Most word-overlapping lessons for this repo (ties: newest first)."""
        task_words = _words(task)
        rows = self.all(repo_key)
        rows.sort(key=lambda r: (len(task_words & _words(r["text"])), r["t"]), reverse=True)
        return [r["text"] for r in rows[:k]]
