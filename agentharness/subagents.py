"""Subagents: delegated tasks with their own context window, tools and (optionally) model.

The parent loop stays in charge. A subagent:
  - starts with a fresh context containing only its definition prompt and the delegated task,
  - may use only the tools its definition lists (never `agent`, so there is no nesting),
  - executes every call through the parent's dispatcher, so permissions, receipts,
    checkpoints and continuous verification apply exactly as for the parent,
  - writes its own transcript and returns only a bounded final report to the parent.

Built-in types: explore, plan, general. Custom agents are Markdown files with a front matter
block, in <project>/.nessa/agents/*.md or ~/.nessa/agents/*.md:

    ---
    name: log-reader
    description: Turns test or compiler output into {error, file, line, cause}
    model: nessa-spec:latest          # optional; default is the parent's subagent model
    tools: read_file, search          # optional; default read-only tools
    max_steps: 6                      # optional
    ---
    You read failing test output and ...
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .llm import ContextOverflow, ToolCall
from .session import bounded_context

READ_TOOLS = ('list_dir', 'search', 'outline', 'read_file', 'instructions_for', 'git', 'show_diff')
EDIT_TOOLS = ('replace_in_file', 'edit_lines', 'write_file', 'undo_file', 'run_check', 'run_command')
NEVER = frozenset({'agent', 'finish', 'propose_plan', 'start_work', 'respond', 'ask_user', 'blocked'})
REPORT_LIMIT = 4000

COMMON = """You are a subagent working for a parent coding agent. You cannot talk to the user and
the parent cannot see your work, only your final report. Act through tool calls. When done,
call report with a concise, factual result: file paths with line numbers, findings, and anything
you could not confirm. Never claim an edit or check happened without a tool result."""


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = READ_TOOLS
    model: str = ''
    max_steps: int = 12
    source: str = 'builtin'


BUILTINS = {
    'explore': AgentDefinition(
        'explore', 'Read-only search: locate files, symbols and behaviour; answer where/how questions.',
        'Search broadly with search/list_dir/outline, then read only the relevant regions. '
        'Report the conclusion with exact paths and line numbers, not file dumps.'),
    'plan': AgentDefinition(
        'plan', 'Read-only design: inspect the code and return a step-by-step implementation plan.',
        'Inspect the relevant code, then report a numbered plan: files to change, the change in each, '
        'risks, and the checks that would verify it. Do not edit anything.'),
    'general': AgentDefinition(
        'general', 'Multi-step work, including edits and checks, on a self-contained task.',
        'Complete the delegated task end to end: inspect, edit with the smallest correct change, '
        'verify with run_check, then report what changed and the check results.',
        READ_TOOLS + EDIT_TOOLS, max_steps=20),
}


def _front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith('---'):
        return {}, text
    head, sep, body = text[3:].partition('\n---')
    if not sep:
        return {}, text
    meta = {}
    for line in head.strip().splitlines():
        key, colon, value = line.partition(':')
        if colon:
            meta[key.strip()] = value.split('#', 1)[0].strip()
    return meta, body.lstrip('\n')


def load_definitions(repo: Path, home: Path | None = None) -> dict[str, AgentDefinition]:
    """Built-ins, then user agents, then project agents (later sources override earlier)."""
    found = dict(BUILTINS)
    home = Path.home() if home is None else home
    for source, folder in (('user', home / '.nessa' / 'agents'), ('project', Path(repo) / '.nessa' / 'agents')):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob('*.md')):
            meta, body = _front_matter(path.read_text(encoding='utf-8', errors='replace'))
            name = meta.get('name') or path.stem
            tools = tuple(t.strip() for t in meta.get('tools', '').split(',') if t.strip()) or READ_TOOLS
            try:
                steps = max(1, min(50, int(meta.get('max_steps', 12))))
            except ValueError:
                steps = 12
            found[name] = AgentDefinition(name, meta.get('description', ''), body.strip(), tools,
                                          meta.get('model', ''), steps, f'{source}:{path}')
    return found


def summary(definitions: dict[str, AgentDefinition]) -> str:
    return '\n'.join(f'- {d.name}: {d.description}' for d in definitions.values())


@dataclass
class SubagentRun:
    """One delegated task. `parent` is the Agent whose dispatcher and evidence it uses."""
    parent: object
    definition: AgentDefinition
    client: object
    task: str
    run_id: str
    read_only: bool = False
    messages: list = field(default_factory=list)
    steps: int = 0

    def offered(self) -> list[str]:
        tools = self.parent.tools
        names = [n for n in self.definition.tools if n in tools and n not in NEVER]
        if self.read_only:
            names = [n for n in names if tools[n].kind == 'read']
        return names

    def run(self) -> tuple[str, str]:
        """Return (status, report)."""
        from .tools import Tool, S
        report_tool = Tool('report', 'Return your final result to the parent agent and stop.',
                           {'result': S}, ('result',), 'control')
        offered = self.offered()
        schemas = [self.parent.tools[n].schema() for n in offered] + [report_tool.schema()]
        names = set(offered) | {'report'}
        system = f'{COMMON}\n\n# Role: {self.definition.name}\n{self.definition.prompt}'
        self.messages = [dict(role='system', content=system),
                         dict(role='user', content=f'Task:\n{self.task}\n\nProject root: . (tool paths are relative to it)')]
        for message in self.messages:
            self._record(message)
        limit = self.parent.config.max_context_chars - len(json.dumps(schemas))
        idle = 0
        while self.steps < self.definition.max_steps:
            self.steps += 1
            digest = dict(task=self.task, note='Earlier subagent turns were compacted; continue from the latest results.')
            self.messages = bounded_context(self.messages, digest, max(4000, limit), keep_last=4)
            try:
                reply = self.client.chat(self.messages, schemas, names)
            except ContextOverflow:
                self.messages = bounded_context(self.messages, digest, max(4000, limit // 2), keep_last=2)
                reply = self.client.chat(self.messages, schemas, names)
            if reply.native:
                envelope = dict(role='assistant', content=reply.content or '', tool_calls=reply.raw_tool_calls)
            else:
                envelope = dict(role='assistant', content=reply.content or '')
            self.messages.append(envelope)
            self._record(envelope)
            if not reply.tool_calls:
                if reply.content.strip():
                    return 'completed', reply.content.strip()  # a plain answer is the report
                idle += 1
                if idle >= 2:
                    return 'stalled', 'The subagent stopped without a report.'
                self._say('Call a tool, or call report with your result.')
                continue
            idle = 0
            results, final = [], None
            for call in reply.tool_calls:
                if final is not None:
                    results.append((call, 'ERROR: deferred; the report was already submitted.'))
                elif call.name == 'report':
                    text = (call.arguments or {}).get('result', '') if isinstance(call.arguments, dict) else ''
                    if text.strip():
                        final = text.strip()
                        results.append((call, 'Report delivered.'))
                    else:
                        results.append((call, 'ERROR: report needs a non-empty result.'))
                else:
                    results.append((call, self.parent._execute(call, offered, dedupe=False)))
            self._deliver(reply, results)
            if final is not None:
                return 'completed', final
        return 'budget_exhausted', f'Stopped after {self.steps} steps without a report.'

    def _say(self, text: str):
        message = dict(role='user', content=text)
        self.messages.append(message)
        self._record(message)

    def _deliver(self, reply, results: list[tuple[ToolCall, str]]):
        limit = self.parent.config.tool_output_chars
        clip = lambda text: text if len(text) <= limit else text[:limit] + f'\n... [{len(text) - limit} chars omitted]'
        if reply.native:
            for call, text in results:
                message = dict(role='tool', tool_call_id=call.id, name=call.name, content=clip(text))
                self.messages.append(message)
                self._record(message)
        elif results:
            self._say('[tool results]\n' + '\n\n'.join(f'### {c.name} result\n{clip(t)}' for c, t in results))

    def _record(self, message: dict):
        self.parent.transcript.write(message, agent=self.run_id)


class Transcript:
    """Append-only conversation transcript: every message, from the parent and each subagent."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: dict, agent: str = 'main', **extra):
        row = dict(t=round(time.time(), 3), agent=agent, **extra, **message)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines() if line.strip()]


def clip_report(text: str) -> str:
    return text if len(text) <= REPORT_LIMIT else text[:REPORT_LIMIT] + '\n... [report truncated]'
