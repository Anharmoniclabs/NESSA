"""Repository-scoped lessons: short, human-reviewed notes injected into future runs.

Only lessons you add (or approve) are stored; the agent never writes here on its own.
"""
from __future__ import annotations

import json
import re
import time
import hashlib
import threading
from collections import Counter
from pathlib import Path

from .session import atomic_json

DEFAULT_PATH = Path.home() / ".agentharness" / "lessons.jsonl"
_LESSON_LOCK = threading.RLock()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower())}


class LessonStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)

    def all(self, repo_key: str | None = None, history: bool = False) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        rows = [r for r in rows if repo_key is None or r["repo"] in (repo_key, "*")]
        if history:
            return rows
        active = {}
        for index, row in enumerate(rows):
            active[(row['repo'], row.get('key', f'legacy-{index}'))] = row
        return [row for row in active.values() if not row.get('deleted')]

    def add(self, repo_key: str, text: str, source: str = "user", *, key: str | None = None,
            deleted: bool = False) -> dict:
        key = key or hashlib.sha256(text.strip().encode()).hexdigest()[:16]
        if not text.strip() or len(text) > 2000 or not re.fullmatch(r'[\w.-]{1,80}', key):
            raise ValueError('Memory needs a key of 1–80 letters/numbers and 1–2000 characters of text.')
        with _LESSON_LOCK:
            prior = [r for r in self.all(repo_key, history=True) if r['repo'] == repo_key and r.get('key') == key]
            row = dict(t=time.time(), repo=repo_key, key=key, text=text.strip(), source=source,
                       version=len(prior) + 1, deleted=deleted,
                       evidence_sha256=hashlib.sha256(text.strip().encode()).hexdigest())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return row

    def forget(self, repo_key: str, key: str) -> dict:
        return self.add(repo_key, 'User removed this memory.', key=key, deleted=True)

    def relevant(self, repo_key: str, task: str, k: int = 5) -> list[str]:
        """Most word-overlapping lessons for this repo (ties: newest first)."""
        task_words = _words(task)
        rows = self.all(repo_key)
        rows.sort(key=lambda r: (len(task_words & _words(r["text"])), r["t"]), reverse=True)
        selected, size = [], 0
        for row in rows:
            width = len(row['text'].encode())
            if size + width <= 1600:
                selected.append(row['text'])
                size += width
            if len(selected) == k:
                break
        return selected


class ConversationMemory:
    """Rebuildable, chat-scoped retrieval cache; never treats model prose as lessons."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def refresh(self, messages: list[dict], query: str, max_chars: int = 3500) -> str:
        # The visible log is authoritative, including turns bypassing Agent.run.
        rows = [dict(turn=i, role=m['role'], content=m['content'])
                for i, m in enumerate(messages)
                if m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)]
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        atomic_json(self.path, dict(version=2, source_sha256=digest, messages=rows))
        if rows and rows[-1]['role'] == 'user' and rows[-1]['content'] == query:
            rows = rows[:-1]
        if not rows:
            return ''
        repeats = Counter(r['content'] for r in rows if r['role'] == 'assistant')
        terms = _words(query)
        feedback = re.compile(r'\b(remember|learn|mistake|correction|stop repeating|doubling|take note|not what|don.t repeat)\b', re.I)
        recent = {r['turn'] for r in rows[-6:]}
        def score(row):
            return (bool(row['role'] == 'user' and feedback.search(row['content'])),
                    row['turn'] in recent,
                    len(terms & _words(row['content'])), row['role'] == 'user', row['turn'])
        selected, seen = [], set()
        remaining = max_chars - 180
        for row in sorted(rows, key=score, reverse=True):
            if row['role'] == 'assistant' and repeats[row['content']] >= 3:
                continue  # Keep repetition in evidence, not as reinforced prompt examples.
            key = (row['role'], row['content'])
            if key in seen:
                continue
            seen.add(key)
            # Excerpts keep the beginning and end of long messages.
            content = row['content']
            if len(content) > 650:
                content = content[:420] + ' … ' + content[-220:]
            line = f"{row['role']} (turn {row['turn'] + 1}): {content}\n"
            if len(line.encode()) <= remaining:
                selected.append((row['turn'], line))
                remaining -= len(line.encode())
        return ('[conversation memory: excerpts from this chat; assistant statements may be wrong]\n'
                + ''.join(line for _, line in sorted(selected)))[:max_chars]
