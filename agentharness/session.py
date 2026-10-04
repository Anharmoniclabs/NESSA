"""Durable receipts and atomic snapshots; never automatically replay an operation."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.operations = self.directory / 'operations'
        self.operations.mkdir(exist_ok=True)

    def begin(self, name: str, arguments, call_id: str, kind: str) -> dict:
        receipt = dict(operation_id=uuid.uuid4().hex, tool_name=name,
                       arguments=arguments, call_id=call_id, kind=kind,
                       status='pending', started_at=time.time())
        self.record(receipt)
        return receipt

    def record(self, receipt: dict) -> None:
        atomic_json(self.operations / (receipt['operation_id'] + '.json'), receipt)

    def finish(self, receipt: dict, output: str, status: str) -> None:
        receipt.update(status=status, finished_at=time.time(), output=output)
        self.record(receipt)

    def uncertain(self) -> list[dict]:
        rows = []
        for path in sorted(self.operations.glob('*.json')):
            row = json.loads(path.read_text())
            if row['status'] in ('pending', 'outcome_unknown'):
                row['status'] = 'outcome_unknown'
                self.record(row)
                rows.append(row)
        return rows

    def save(self, state: dict) -> None:
        atomic_json(self.directory / 'session.json', state)

    def load(self) -> dict:
        state = json.loads((self.directory / 'session.json').read_text())
        if state.get('version') != 1:
            raise ValueError('Unsupported session format')
        return state


def message_groups(messages: list[dict]) -> list[list[dict]]:
    """Keep an assistant tool-call envelope and all its results indivisible."""
    groups = []
    for message in messages:
        if message['role'] == 'tool' and groups:
            groups[-1].append(message)
        elif message['role'] == 'user' and str(message.get('content', '')).startswith('[tool results]') and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def bounded_context(messages: list[dict], digest: dict, max_chars: int,
                    keep_last: int = 6) -> list[dict]:
    """Drop whole historical exchanges. Never alter a call's arguments or IDs.

    Character accounting bounds serialized payload, not exact backend tokens.
    The complete conversation remains in the durable session/event records.
    """
    size = lambda rows: len(json.dumps(rows, ensure_ascii=False))
    if size(messages) <= max_chars:
        return messages
    prefix = [messages[0], {'role': 'user', 'content': '[session digest]\n' +
                           json.dumps(digest, ensure_ascii=False)}]
    if size(prefix) > max_chars:
        raise ValueError('Task, instructions and digest exceed context budget; narrow the task')
    groups = message_groups(messages[1:])[-keep_last:]
    while groups and size(prefix + [m for g in groups for m in g]) > max_chars:
        groups.pop(0)
    return prefix + [m for g in groups for m in g]
