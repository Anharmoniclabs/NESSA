"""Plain-English narration of what the agent is doing, built from harness events.

This is deterministic: it reports what actually happened (tool calls, checks, model switches)
and the model's own reasoning text when the model returns one. It never asks another model to
guess at reasoning, so it adds no latency and cannot describe steps that did not happen.
"""
from __future__ import annotations

import re

TOOL_PHRASES = {
    'read_file': 'Reading {path}', 'list_dir': 'Looking at the files in {path}', 'search': 'Searching for "{pattern}"',
    'outline': 'Skimming the structure of {path}', 'replace_in_file': 'Editing {path}', 'edit_lines': 'Editing {path}',
    'write_file': 'Writing {path}', 'undo_file': 'Reverting {path}', 'show_diff': 'Reviewing the changes so far',
    'run_check': 'Running the {name} check', 'run_command': 'Running `{command}`', 'dev_start': 'Launching {name}',
    'dev_status': 'Checking whether {name} is running', 'dev_logs': 'Reading the output of {name}',
    'dev_stop': 'Stopping {name}', 'dev_health': 'Checking that {name} responds', 'git': 'Checking git {command}',
    'git_commit': 'Committing: "{message}"', 'web_search': 'Searching the web for "{query}"',
    'news_search': 'Looking for news about "{query}"', 'web_fetch': 'Reading {url}', 'weather': 'Checking the weather in {location}',
    'agent': 'Handing a subtask to the {agent_type} helper', 'start_work': 'Moving into work mode',
    'propose_plan': 'Proposing a plan', 'finish': 'Wrapping up', 'respond': 'Answering', 'use_skill': 'Following the {name} recipe',
}


def _tool(name: str, args: dict) -> str:
    template = TOOL_PHRASES.get(name)
    if template is None:
        return f'Using {name}'
    try:
        return template.format(**{k: str(v)[:80] for k, v in (args or {}).items()} | {'path': (args or {}).get('path', '.')})
    except (KeyError, IndexError):
        return re.sub(r'\s*[`"]?\{\w+\}[`"]?', '', template)


def narrate(event: str, data: dict) -> str | None:
    """One line for an event, or None when it is not worth showing."""
    d = data or {}
    if event == 'model_started':
        return f'Thinking ({d.get("model") or "model"})…'
    if event == 'model':
        calls = d.get('calls') or []
        if calls:
            return 'Decided to: ' + '; '.join(_tool(c['name'], c.get('args') or {}) for c in calls[:4])
        return None
    if event == 'tool':
        output = str(d.get('output', ''))
        if output.startswith('ERROR:'):
            return f'  ↳ {d.get("name")} failed: {output[6:120].strip()}'
        return None
    if event == 'check':
        phase = {'baseline': ' (before changes)', 'continuous': ' (after edit)'}.get(d.get('phase'), '')
        return f'  ↳ {d.get("name")} check {d.get("status")}{phase}'
    if event == 'permission' and not d.get('allowed'):
        return f'  ↳ {d.get("name")} not allowed'
    if event == 'model_switched':
        return f'{d.get("previous")} unavailable — switched to {d.get("model")}'
    if event == 'subagent_started':
        return f'Started the {d.get("agent_type")} helper ({d.get("model")})'
    if event == 'subagent_finished':
        return f'  ↳ helper {d.get("status")} after {d.get("steps")} steps'
    if event == 'plan':
        steps = (d.get('plan') or {}).get('steps') or []
        return 'Planned: ' + '; '.join(str(s)[:70] for s in steps[:4])
    if event == 'work_requested':
        return 'Starting work on the request'
    if event == 'scratch_folder':
        return f'Working in {d.get("path")}'
    if event == 'context_compacted':
        return 'Summarised older messages to stay within the context window'
    return None


def latest_reasoning(activity: list[dict], limit: int = 600) -> str:
    """The model's own reasoning from its most recent reply, when the model provides one."""
    for entry in reversed(activity):
        text = str((entry.get('data') or {}).get('reasoning') or '').strip() if entry.get('event') == 'model' else ''
        if text:  # replies that only call a tool often carry no reasoning; keep the latest that does
            return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + '…'
    return ''


def feed(activity: list[dict], last: int = 8) -> list[str]:
    lines = [line for line in (narrate(e.get('event', ''), e.get('data') or {}) for e in activity) if line]
    return lines[-last:]
