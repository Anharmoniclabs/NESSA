"""Browser evidence: capture what a page does wrong, then replay after a fix and compare.

Works through the Chrome DevTools MCP adapter (`[mcp.chrome-devtools]`), so it needs Node.js and a
Chrome. A capture records console errors/warnings and failed network requests as normalized
strings (no per-run ids), saved under the run's evidence directory. A replay captures again and
reports which problems were resolved, remain, or are new. Only local pages are captured: this is
a development loop, not a crawler.

Parsers target chrome-devtools-mcp's text output as observed in the 2026-10-05 validation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .workspace import ToolError

LOCAL_URL = re.compile(r'https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)')
LABEL = re.compile(r'[\w.-]{1,60}')
CONSOLE_LINE = re.compile(r'^msgid=\d+ \[(\w+)\] (.*?)(?: \(\d+ args\))?(?: \[\d+ times\])?$')
NETWORK_LINE = re.compile(r'^reqid=\d+ (\S+) (\S+) \[([^\]]+)\]')
PAGE_LINE = re.compile(r'^(\d+): (.*) \((\S+)\) \[selected\]$')
PROBLEM_LEVELS = ('error', 'warn')
Call = Callable[[str, dict], str]


class BrowserError(ToolError):
    pass


@dataclass
class BrowserEvidence:
    url: str
    label: str
    title: str = ''
    console: list = field(default_factory=list)   # "error: message", duplicates collapsed, order kept
    failed_requests: list = field(default_factory=list)  # "GET http://... 404"
    captured_at: float = field(default_factory=time.time)

    @property
    def problems(self) -> set:
        return set(self.console) | set(self.failed_requests)


def parse_selected_page(text: str) -> tuple[int, str]:
    for line in text.splitlines():
        found = PAGE_LINE.match(line.strip())
        if found:
            return int(found[1]), found[2]
    raise BrowserError('the browser did not report a selected page')


def parse_console(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in text.splitlines():
        found = CONSOLE_LINE.match(line.strip())
        if found and found[1] in PROBLEM_LEVELS:
            seen[f'{found[1]}: {found[2]}'] = None
    return list(seen)


def parse_network(text: str) -> list[str]:
    """Requests that failed: HTTP status 400 or above, or no numeric status (network failure)."""
    failed: dict[str, None] = {}
    for line in text.splitlines():
        found = NETWORK_LINE.match(line.strip())
        if found and not (found[3].isdigit() and int(found[3]) < 400):
            failed[f'{found[1]} {found[2]} {found[3]}'] = None
    return list(failed)


def _checked(output: str, what: str) -> str:
    if output.startswith('ERROR'):
        raise BrowserError(f'{what} failed: {output[:300]}')
    return output


def capture(call: Call, url: str, label: str) -> BrowserEvidence:
    """Open `url` in a fresh tab, collect its problems, close the tab. `call(tool, args)` runs one adapter tool."""
    if not LOCAL_URL.match(url):
        raise BrowserError('only local http(s) pages (localhost, 127.0.0.1, [::1]) can be captured')
    if not LABEL.fullmatch(label):
        raise BrowserError('label must be 1-60 letters, numbers, dot, dash or underscore')
    page, title = parse_selected_page(_checked(call('new_page', {'url': url}), 'opening the page'))
    try:
        console = parse_console(_checked(call('list_console_messages', {'pageId': page}), 'reading console'))
        failed = parse_network(_checked(call('list_network_requests', {'pageId': page}), 'reading network'))
    finally:
        call('close_page', {'pageId': page})  # best effort: a failed close must not hide the capture
    return BrowserEvidence(url, label, title, console, failed)


@dataclass
class Replay:
    before: str
    after: str
    resolved: list
    remaining: list
    introduced: list

    @property
    def verdict(self) -> str:
        if self.introduced:
            return 'regressed'
        if self.remaining:
            return 'improved' if self.resolved else 'unchanged'
        return 'fixed' if self.resolved else 'no_problems_seen'

    def render(self) -> str:
        rows = [f'Replay of {self.before!r} as {self.after!r}: {self.verdict}']
        for title, items in (('Resolved', self.resolved), ('Still present', self.remaining),
                             ('New', self.introduced)):
            if items:
                rows.append(f'{title}:\n' + '\n'.join(f'  - {i}' for i in items))
        return '\n'.join(rows)


def compare(before: BrowserEvidence, after: BrowserEvidence) -> Replay:
    old, new = before.problems, after.problems
    return Replay(before.label, after.label, sorted(old - new), sorted(old & new), sorted(new - old))


def save(directory: Path, evidence: BrowserEvidence) -> Path:
    directory = Path(directory) / 'browser'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{evidence.label}.json'
    path.write_text(json.dumps(asdict(evidence), indent=2))
    return path


def load(directory: Path, label: str) -> BrowserEvidence:
    if not LABEL.fullmatch(label):
        raise BrowserError('label must be 1-60 letters, numbers, dot, dash or underscore')
    path = Path(directory) / 'browser' / f'{label}.json'
    try:
        return BrowserEvidence(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        raise BrowserError(f'no saved capture named {label!r}') from exc


def describe(evidence: BrowserEvidence) -> str:
    lines = [f'Captured {evidence.url} ({evidence.title or "untitled"}) as {evidence.label!r}: '
             f'{len(evidence.console)} console problem(s), {len(evidence.failed_requests)} failed request(s).']
    lines += [f'  console {c}' for c in evidence.console] + [f'  network {r}' for r in evidence.failed_requests]
    return '\n'.join(lines)


PREFIX = 'mcp__chrome-devtools__'
REQUIRED_TOOLS = ('new_page', 'list_console_messages', 'list_network_requests', 'close_page')


def available(bus) -> bool:
    """True when the configured MCP bus exposes every adapter tool the evidence loop needs."""
    return bus is not None and all(PREFIX + name in bus.discovered for name in REQUIRED_TOOLS)


def _adapter(ctx) -> Call:
    return lambda tool, args: ctx.mcp.call(PREFIX + tool, args)


def _capture_tool(ctx, args) -> str:
    evidence = capture(_adapter(ctx), args['url'], args['label'])
    save(ctx.evidence_dir, evidence)
    return describe(evidence)


def _replay_tool(ctx, args) -> str:
    before = load(ctx.evidence_dir, args['baseline'])
    after = capture(_adapter(ctx), args['url'], args['label'])
    save(ctx.evidence_dir, after)
    return describe(after) + '\n\n' + compare(before, after).render()


def tools() -> dict:
    from .tools import S, Tool
    return {
        'browser_capture': Tool(
            'browser_capture', 'Open a LOCAL page in the development browser and record its console errors/warnings '
            'and failed requests as named evidence. Capture before a fix so it can be replayed after.',
            {'url': S, 'label': {**S, 'description': 'name for this capture, e.g. before-fix'}},
            ('url', 'label'), 'mcp', handler=_capture_tool, cacheable=False),
        'browser_replay': Tool(
            'browser_replay', 'Capture the same page again as a new label and compare with an earlier capture: '
            'resolved, still present and new problems. Run after a fix.',
            {'url': S, 'label': {**S, 'description': 'name for this capture, e.g. after-fix'},
             'baseline': {**S, 'description': 'label of the earlier capture to compare against'}},
            ('url', 'label', 'baseline'), 'mcp', handler=_replay_tool, cacheable=False),
    }
