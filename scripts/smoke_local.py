"""Real-model acceptance: fix a bug in a private copy and independently check behavior."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.llm import ChatClient
from agentharness.profiles import PROFILES
from agentharness.workspace import Workspace


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', choices=PROFILES, default='laptop-i3-12gb')
    p.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    p.add_argument('--model')
    p.add_argument('--out', type=Path)
    p.add_argument('--conversation', action='store_true', help='verify chat, memory and coding in one conversation')
    args = p.parse_args()
    profile = PROFILES[args.profile]
    client = ChatClient(args.base_url, args.model or profile['model'], max_tokens=profile['max_tokens'],
                        temperature=profile.get('temperature', 0.0),
                        reasoning_effort=profile.get('reasoning_effort'))
    if client.model not in client.models():
        raise SystemExit(f'Model not served: {client.model}')
    root = args.out or Path.home() / '.agentharness' / 'smoke' / str(time.time_ns())
    root.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source'
        source.mkdir()
        original = 'def add(a, b):\n    return a - b\n'
        (source / 'calc.py').write_text(original)
        ws = Workspace.create(source, root / 'work')
        # This check lives in the controller, outside the model-editable snapshot.
        def behavior(workspace):
            import subprocess
            from agentharness.checks import CheckResult
            script = 'from calc import add; assert add(2,3)==5; assert add(-4,1)==-3; assert add(0,7)==7'
            r = subprocess.run([sys.executable, '-B', '-c', script], cwd=workspace.repo,
                               capture_output=True, text=True, timeout=15)
            return CheckResult('tests', 'passed' if r.returncode == 0 else 'failed',
                               exit_code=r.returncode, output=r.stdout + r.stderr)
        config = AgentConfig(require_approval=False, conversational=args.conversation, max_steps=16, time_budget=900,
                             tool_mode='text' if profile['text_tools'] else 'native',
                             max_context_chars=profile['max_context_chars'],
                             tool_output_chars=profile['tool_output_chars'], allow_shell=False, allow_extract=False)
        checks = CheckRunner({'syntax': syntax_check, 'tests': behavior})
        conversation = []
        if args.conversation:
            for index, message in enumerate(('Hi, my name is alabs. Say hello briefly.',
                                             'What is my name? Answer only the name.')):
                turn = Agent(client, ws, config=config, checks=checks).run(
                    message, resume=index > 0, message=message if index else '')
                conversation.append({'message': message, 'status': turn.status, 'reply': turn.summary,
                                     'seconds': turn.seconds, 'checks': turn.checks,
                                     'changed_files': turn.changed_files})
                if turn.status != 'answered' or turn.changed_files or any(turn.checks.values()):
                    break
        conversation_passed = not args.conversation or (
            len(conversation) == 2 and all(t['status'] == 'answered' and not t['changed_files']
                                          and not any(t['checks'].values()) for t in conversation)
            and 'alabs' in conversation[-1]['reply'].lower())
        task = ('Fix add(a, b) in calc.py: it should return the sum. Inspect the file, propose a plan, '
                'make the smallest correction, run tests, and finish.')
        result = Agent(client, ws, config=config, checks=checks).run(
            task, resume=args.conversation, message=task if args.conversation else '')
        passed = conversation_passed and result.status == 'verified' and behavior(ws).status == 'passed' and (source / 'calc.py').read_text() == original
        report = {'passed': passed, 'model': client.model, 'status': result.status,
                  'steps': result.steps, 'seconds': result.seconds, 'evidence': result.evidence_dir,
                  'profile': args.profile, 'backend': args.base_url}
        if args.conversation:
            report['conversation'] = conversation
            report['conversation_passed'] = conversation_passed
        (root / 'acceptance.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
