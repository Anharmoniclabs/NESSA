"""Scrollback-friendly terminal UI with stdlib readline and safe streaming output."""
from __future__ import annotations
import os
import re
import shutil
import sys
import time
from .narrate import narrate

COMMANDS = ('/help', '/status', '/apps', '/skills', '/production', '/approve-image', '/graph', '/diff', '/paste', '/clear', '/apply', '/quit')


def safe(text):
    # Strip terminal control bytes from model, file and tool output (including OSC).
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(text))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(c for c in str(text) if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) <= 159).replace('\x1b', '')


class TerminalUI:
    def __init__(self, project, work, client, direct, plain=False):
        self.project, self.work, self.client, self.direct = project, work, client, direct
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty() and not plain and os.environ.get('TERM') != 'dumb'
        self.color = self.interactive and 'NO_COLOR' not in os.environ
        self.streamed = ''
        self.open_reply = False
        self.calls = 0
        self.last_metrics = {}
        self.started = time.monotonic()
        self.readline = None
        self.old_completer = None
        if self.interactive:
            try:
                import readline
                self.readline = readline
                self.old_completer = readline.get_completer()
                readline.set_completer(self.complete)
                readline.parse_and_bind('tab: complete')
            except ImportError:
                pass

    def complete(self, text, state):
        matches = [c for c in COMMANDS if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    def style(self, text, code='36'):
        text = safe(text)
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def line(self, text='', code=None):
        self.end_stream()
        print(self.style(text, code) if code else safe(text), flush=True)

    def header(self):
        width = max(20,min(88,shutil.get_terminal_size().columns)-2)
        self.line('╭'+'─'*width+'╮', '36')
        self.line('  NESSA  /  terminal workspace', '1;36')
        self.line(f'  {self.project}')
        self.line(f'  {getattr(self.client,"model","model")} · {"edits with permissions" if self.direct else "private working copy"}', '2')
        self.line('  /help commands · /graph project memory · Ctrl-C stop · /quit exit', '2')
        self.line('╰'+'─'*width+'╯', '36')

    def read(self):
        if not self.interactive:
            return input('you> ').strip()
        self.line()
        self.line('─'*max(10,min(88,shutil.get_terminal_size().columns)), '2')
        prompt = '\001\033[1;36m\002❯ \001\033[0m\002' if self.color else '❯ '
        return input(prompt).strip()

    def begin(self):
        self.started = time.monotonic()
        self.streamed = ''

    def delta(self, text):
        if not self.open_reply:
            print('\n'+self.style('nessa', '1;36'), flush=True)
            self.open_reply = True
        clean = safe(text)
        self.streamed += clean
        print(clean, end='', flush=True)

    def end_stream(self):
        if self.open_reply:
            print('', flush=True)
            self.open_reply = False

    def event(self, event, data):
        if event == 'model_started':
            self.end_stream()
            self.streamed = ''
            self.calls += 1
        if event == 'model':
            self.last_metrics = data.get('inference') or {}
        note = narrate(event, data)
        if note:
            self.line('  · '+note, '2')
        if event == 'graph_retrieval':
            self.line(f'  · Retrieved project graph evidence ({data.get("chars",0)} characters)', '2')

    def answer(self, result):
        self.end_stream()
        if safe(result.summary).strip() != self.streamed.strip():
            self.line('\nnessa', '1;36')
            self.line(result.summary)
        self.line(f'  {result.status} · {time.monotonic()-self.started:.1f}s', '2')

    def status(self):
        self.line(f'Model: {getattr(self.client,"model","unknown")}\nProject: {self.project}\nSession: {self.work}')
        m = self.last_metrics
        self.line(f'Requests this session: {self.calls} · last input/output tokens: {m.get("prompt_tokens","—")}/{m.get("completion_tokens","—")} · cached: {m.get("cached_tokens","—")}')
        if m.get('ttft_seconds') is not None:
            self.line(f'Last first generation event: {m["ttft_seconds"]:.2f}s')

    def command(self, message, ws):
        if message.startswith('/approve-image '):
            from .media_approval import approve_image
            record=approve_image(ws,message[len('/approve-image '):].strip())
            self.line('Approved for animation: '+record['path']+' (SHA256 '+record['sha256'][:12]+'). Image changes require new approval.')
        elif message == '/approve-image':
            self.line('After inspecting the image: /approve-image <project-relative PNG/JPEG path>')
        elif message == '/help':
            self.line('/approve-image <path>  Approve the exact image for animation after reviewing it')
            self.line('/graph [question]  Inspect or search project relationships\n/production       Studio jobs, assets and trials\n/apps             Installed creative engines\n/skills           Available workflows\n/status           Model, session and usage\n/diff             Review current patch\n/paste            Multiline input; finish with /send\n/clear            Redraw screen (conversation retained)\n/apply            Apply private-workspace changes\n/quit             Exit; session evidence is retained')
        elif message == '/production':
            import json
            from .production import Production
            self.line(json.dumps(Production(ws.repo).status(),indent=2))
        elif message == '/apps':
            from .creative import inventory
            self.line(inventory())
        elif message == '/skills':
            from .skills import SkillRegistry
            self.line(SkillRegistry(ws.repo).summary())
        elif message == '/status':
            self.status()
        elif message == '/diff':
            self.line(ws.patch() or 'No changes.')
        elif message == '/clear':
            if self.interactive:
                print('\033[2J\033[H', end='')
            self.header()
        elif message == '/graph' or message.startswith('/graph '):
            from .graph import GraphIndex
            graph = GraphIndex(ws.repo)
            if message == '/graph':
                stats = graph.refresh()
                self.line(f'Project graph: {stats["files"]} files · {stats["nodes"]} nodes · {stats["edges"]} relationships\nUse /graph <question> to retrieve cited evidence.')
            else:
                self.line(graph.retrieve(message[7:]) or 'No matching indexed project evidence.')
        else:
            return False
        return True

    def ask(self, name, args):
        self.line('\nPermission requested: '+name, '1;33')
        for key,value in args.items():
            self.line(f'  {key}: {str(value)[:1200]}')
        answer = input('Allow? [y] once / [a] always for this tool / [n] deny: ').strip()
        return (answer.lower() in ('y','yes','a','always'),
                '' if answer.lower() in ('y','yes','a','always','n','no','') else answer,
                answer.lower() in ('a','always'))

    def approve(self, plan):
        self.line('\nProposed plan', '1;33')
        self.line(plan.get('goal',''))
        for i,step in enumerate(plan.get('steps',[]),1):
            self.line(f'  {i}. {step}')
        self.line('Files: '+', '.join(plan.get('files',[])))
        self.line('Checks: '+', '.join(plan.get('checks',[])))
        answer = input('Approve? [y] yes / [n] no / feedback: ').strip()
        return answer.lower() in ('y','yes'), '' if answer.lower() in ('y','yes','n','no','') else answer

    def close(self):
        self.end_stream()
        if self.readline is not None:
            self.readline.set_completer(self.old_completer)
