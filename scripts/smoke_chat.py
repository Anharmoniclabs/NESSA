#!/usr/bin/env python3
"""Exercise real desktop inference; save replies, errors and heuristic checks.

No scripted model replies. A passing smoke check is not a general quality score.
"""
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
    parser.add_argument('--base-url', default='http://127.0.0.1:11435/v1')
    parser.add_argument('--model', default='nessa-lfm:latest')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.output or Path(tempfile.mkdtemp(prefix='nessa-chat-acceptance-'))
    root.mkdir(parents=True, exist_ok=True)
    app = App(root / 'chats', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest',
              chat_model=args.model, chat_base_url=args.base_url)
    chat_id = app.create()['id']
    turns = []
    cases = [
        ('hey', None),
        ('write me a book', r'genre|topic|book|story|novel|outline|fiction|about'),
        ('A fantasy book about a clockmaker named Mira. Write its opening scene now.', r'\bMira\b'),
        ('What is the name of the clockmaker?', r'\bMira\b'),
    ]
    for prompt, expected in cases:
        started = time.monotonic()
        app.send(chat_id, prompt)
        deadline = started + 90
        while True:
            state = app.snapshot(chat_id)
            if not state['busy'] or state['plan'] or time.monotonic() >= deadline:
                break
            time.sleep(.1)
        reply = state['messages'][-1]
        text = reply.get('content', '') if reply['role'] == 'assistant' else ''
        checks = {
            'finished': not state['busy'],
            'answered': reply.get('status') == 'answered',
            'no_project_approval': state['plan'] is None,
            'nonempty': bool(text.strip()),
            'no_generic_refusal': not re.search(
                r"(?:can(?:not|'t|’t)|unable to)\s+(?:assist|help|write)|"
                r"(?:not able to|not allowed to)\s+(?:assist|help|write)", text, re.I),
            'topic_present': expected is None or bool(re.search(expected, text, re.I)),
        }
        turns.append(dict(prompt=prompt, reply=text, checks=checks,
                          seconds=round(time.monotonic() - started, 2)))
        print(json.dumps(turns[-1]), flush=True)
        if not all(checks.values()):
            app.stop(chat_id)
            break
    passed = len(turns) == len(cases) and all(all(t['checks'].values()) for t in turns)
    report = dict(passed=passed, model=args.model, base_url=args.base_url, turns=turns,
                  note='Heuristic live desktop smoke check; inspect replies for writing quality.')
    destination = root / 'acceptance.json'
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(f'{"PASS" if passed else "FAIL"}: {destination}')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
