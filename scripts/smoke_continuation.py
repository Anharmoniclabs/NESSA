#!/usr/bin/env python3
"""Live romance/continuation acceptance. Run outside a socket-restricted sandbox."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentharness.desktop import App


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.output or Path(tempfile.mkdtemp(prefix='nessa-continuation-'))
    root.mkdir(parents=True, exist_ok=True)
    app = App(root / 'chats', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
    key = app.create()['id']
    prompts = [
        'Write a romance about an adult knight named Rowan and an adult princess named Mira. '
        'The setting is a castle on an abandoned island guarded by a dragon. '
        'Outline the full story setting and twelve chapters in several short passes. '
        'Give the setting first, then the chapters, and keep going until the outline is complete.',
        'Continue from where you left off if the outline is unfinished. Otherwise write the opening of chapter one.',
        'What are the names of the two main characters, and where is the castle?',
    ]
    turns = []
    for prompt in prompts:
        before = len(app.snapshot(key)['messages'])
        started = time.monotonic()
        app.send(key, prompt)
        while app.snapshot(key)['busy'] and time.monotonic() - started < 780:
            if app.snapshot(key)['plan']:
                break
            time.sleep(.2)
        state = app.snapshot(key)
        replies = [m for m in state['messages'][before:] if m['role'] == 'assistant']
        text = '\n\n'.join(m['content'] for m in replies)
        result = state.get('result') or {}
        checks = dict(finished=not state['busy'], no_approval=state['plan'] is None,
                      saved_answer=result.get('status') in ('answered', 'awaiting_continuation'),
                      nonempty=bool(text), no_false_limit=not re.search(
                          r"exceeded.{0,20}character limit|start fresh|model server error", text, re.I))
        if len(turns) == 2:
            checks['recall'] = all(word in text.lower() for word in ('rowan', 'mira', 'island'))
        row = dict(prompt=prompt, replies=replies, checks=checks,
                   seconds=round(time.monotonic() - started, 2))
        turns.append(row)
        print(json.dumps(row), flush=True)
        if not all(checks.values()):
            app.stop(key)
            break
        # Each follow-up uses a new App instance and the persisted model history.
        app = App(root / 'chats', app.base_url, app.model)
    passed = len(turns) == len(prompts) and all(all(t['checks'].values()) for t in turns)
    report = dict(passed=passed, turns=turns,
                  note='Live heuristic check. Read the outline to judge completeness and consistency; '
                       'this does not prove story quality or perfect long-term recall.')
    (root / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'{"PASS" if passed else "FAIL"}: {root / "acceptance.json"}')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
