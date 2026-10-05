#!/usr/bin/env python3
"""Replay a copy of a desktop conversation against the local model; preserve the original."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentharness.desktop import App


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('chat', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prompt', action='append', help='override the default replay questions')
    args = parser.parse_args()
    root = args.output or Path(tempfile.mkdtemp(prefix='nessa-memory-smoke-'))
    app = App(root / 'chats', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
    key = app.create()['id']
    original = json.loads(args.chat.read_text())
    chat = app.chats[key]
    chat.data['messages'] = original['messages']
    # Include the real model's previous internal history as well as the UI log.
    old_work = Path(original.get('work_directory', args.chat.parent))
    session = old_work / 'evidence/session.json'
    if session.exists():
        destination = chat.directory / 'evidence/session.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(session.read_text())
    chat.save()
    for prompt in (args.prompt or ('Are you human? Correct your earlier explanation in two sentences.',
                   'Remember: stop repeating your introduction; answer my technical questions directly.',
                   'How does matrix multiplication produce contextual responses in an LLM?')):
        app.send(key, prompt)
        started = time.monotonic()
        while app.snapshot(key)['busy'] and time.monotonic() - started < 240:
            time.sleep(.2)
        state = app.snapshot(key)
        print(json.dumps(dict(prompt=prompt, reply=state['messages'][-1],
                              busy=state['busy'], seconds=round(time.monotonic()-started, 1),
                              first_token_seconds=state.get('first_token_seconds'),
                              prompt_budgets=[e['data'] for e in state['activity'] if e['event'] == 'prompt_budget'][-1:])), flush=True)
        if state['busy'] or state['messages'][-1].get('status') != 'answered':
            app.stop(key)
            return 1
        answer = state['messages'][-1]['content']
        if re.search(r"I(?:'m| am) (?:a )?human|can(?:not|'t) assist with that", answer, re.I):
            print('FAIL: inspect the refusal or false identity claim above.', flush=True)
            return 1
    print(f'Review live replies in {chat.directory / "chat.json"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
