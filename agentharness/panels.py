"""Plain-text panels for the desktop details window, built from the live activity feed.

Pure functions over the event list (and the run's evidence directory for browser captures), so
they are tested without a display. The feed keeps only the newest events; panels say what they
could see rather than implying a complete history.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

CHECK_FAILING = ('failed', 'timeout', 'setup_error', 'error')
INTEGRITY = ('stale_receipt', 'repeated_failure', 'dev_stop_failed')
DEV_TOOLS = ('dev_start', 'dev_status', 'dev_logs', 'dev_health', 'dev_wait', 'dev_stop')


def _clock(event: dict) -> str:
    return time.strftime('%H:%M:%S', time.localtime(event.get('time', 0)))


def _clip(text, limit=110) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _line(event: dict) -> str | None:
    """One timeline row for an event that matters to a person; None for internal chatter."""
    name, data = event['event'], event.get('data') or {}
    if name == 'tool':
        return f"tool {data.get('name')} {_clip(json.dumps(data.get('args', {}), ensure_ascii=False), 70)}"
    if name == 'check':
        return f"check {data.get('name')} {data.get('status')} ({data.get('phase')})"
    if name == 'plan':
        return f"plan proposed: {_clip((data.get('plan') or {}).get('goal', ''))}"
    if name == 'approval':
        return f"approval {'granted' if data.get('approved') else 'declined'}"
    if name == 'skill':
        return f"skill {data.get('name', '')} activated"
    if name == 'review':
        return f"reviewer notes ({data.get('phase')})"
    if name == 'checkpoint':
        return f"checkpoint {data.get('edit')}: {', '.join(data.get('changed') or [])}"
    if name == 'context_compacted':
        return 'context compacted'
    if name in INTEGRITY or name == 'verification_receipt':
        return name.replace('_', ' ')
    if name == 'end':
        return f"run ended: {data.get('status')}"
    return None


def timeline(activity: list[dict]) -> str:
    rows = [f'{_clock(e)}  {line}' for e in activity if (line := _line(e))]
    return '\n'.join(rows) or 'No steps yet.'


def checks(activity: list[dict], result: dict | None = None, evidence_dir: str = '') -> str:
    """Latest status per check, the verification receipt, integrity events and browser captures."""
    latest: dict[tuple, dict] = {}
    for e in activity:
        if e['event'] == 'check':
            d = e['data']
            latest[(d.get('phase'), d.get('name'))] = d
    sections = []
    if latest:
        sections.append('CHECKS\n' + '\n'.join(
            f"{'✗' if d.get('status') in CHECK_FAILING else '✓'} {name} · {phase} · {d.get('status')}"
            + (f" · exit {d.get('exit_code')}" if d.get('exit_code') not in (None, 0) else '')
            for (phase, name), d in latest.items()))
    receipt = next((e['data'] for e in reversed(activity) if e['event'] == 'verification_receipt'), None)
    if receipt:
        sections.append('VERIFICATION RECEIPT\n' + f"workspace {receipt.get('workspace_sha', '')[:16]}\n"
                        f"checks    {receipt.get('checks_sha', '')[:16]}\n"
                        + ', '.join(f'{k}={v}' for k, v in (receipt.get('statuses') or {}).items()))
    flags = [f"{_clock(e)}  {e['event'].replace('_', ' ')}" for e in activity if e['event'] in INTEGRITY]
    if flags:
        sections.append('INTEGRITY\n' + '\n'.join(flags))
    if result and result.get('status'):
        sections.append(f"RESULT\n{result['status']}")
    browser = _browser(evidence_dir)
    if browser:
        sections.append('BROWSER CAPTURES\n' + browser)
    return '\n\n'.join(sections) or 'No checks have run yet.'


def _browser(evidence_dir: str) -> str:
    if not evidence_dir:
        return ''
    rows = []
    for path in sorted((Path(evidence_dir) / 'browser').glob('*.json')):
        try:
            saved = json.loads(path.read_text())
            rows.append(f"{saved['label']} · {saved['url']} · {len(saved.get('console', []))} console, "
                        f"{len(saved.get('failed_requests', []))} failed requests")
        except (OSError, ValueError, KeyError):
            continue
    return '\n'.join(rows)


def context(activity: list[dict]) -> str:
    budgets = [e['data'] for e in activity if e['event'] == 'prompt_budget']
    if not budgets:
        return 'No model request has been made yet.'
    last = budgets[-1]
    compactions = sum(1 for e in activity if e['event'] == 'context_compacted')
    return (f"Model: {last.get('model')}\nLast prompt: {last.get('bytes', 0):,} bytes, "
            f"{last.get('tools')} tools offered\nModel requests seen: {len(budgets)}\n"
            f"Compactions seen: {compactions}\n(Bytes of serialized messages and tool schemas, not tokens.)")


def reviewer(activity: list[dict]) -> str:
    notes = [f"{_clock(e)}  {e['data'].get('phase')} review (edit {e['data'].get('edit')}, "
             f"{e['data'].get('status')})\n{e['data'].get('notes', '')}"
             for e in activity if e['event'] == 'review']
    return ('Advisory only: the reviewer cannot verify or complete work.\n\n' + '\n\n'.join(notes)) if notes \
        else 'No reviewer notes. A review model is optional and off by default.'


def processes(activity: list[dict]) -> str:
    """Dev-process and skill activity as it appeared in the feed (not live process state)."""
    rows = []
    for e in activity:
        data = e.get('data') or {}
        if e['event'] == 'tool' and data.get('name') in DEV_TOOLS:
            rows.append(f"{_clock(e)}  {data['name']} {_clip(json.dumps(data.get('args', {})), 60)}\n"
                        f"          {_clip(data.get('output', ''), 140)}")
        elif e['event'] == 'skill':
            rows.append(f"{_clock(e)}  skill {data.get('name', '')}")
    return '\n'.join(rows) or 'No dev processes or skills used in the recent activity.'
