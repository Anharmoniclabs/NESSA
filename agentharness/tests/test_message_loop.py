"""Behavioral tests: feedback, restart, context bounds and honest completion."""
import json
import sys
import unittest

from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckResult, CheckRunner, syntax_check
from agentharness.llm import ChatClient, ModelError, ToolCall
from agentharness.session import bounded_context
from agentharness.tools import build_tools
from agentharness.workspace import ToolError, Workspace
from agentharness.tests.test_agent import Base, ScriptedClient, BUGGY, UNITTEST
from agentharness.tests.test_http_cli import FakeServer


class MessageLoop(Base):
    def test_tool_result_is_observed_before_next_decision(self):
        def respond(messages):
            self.assertIn('return a - b', messages[-1]['content'])
            self.assertEqual(messages[-1]['tool_call_id'], 'c0')
            return [('respond', {'message': 'The function subtracts.'})]
        result = self.agent([[('read_file', {'path': 'calc.py'})], respond],
                            plan_first=False).run('Explain calc.py')
        self.assertEqual(result.status, 'answered')
        self.assertEqual(result.patch, '')

    def test_clarification_resumes_existing_workspace_and_history(self):
        first = self.agent([[('ask_user', {'question': 'Which function?'})]], plan_first=False)
        result = first.run('Fix the arithmetic')
        self.assertEqual(result.status, 'awaiting_input')
        client = ScriptedClient([[('read_file', {'path': 'calc.py'})],
            [('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'})],
            [('finish', {'summary': 'Fixed add'})]])
        before = len((first.evidence_dir / 'events.jsonl').read_text().splitlines())
        restored = Agent(client, Workspace(self.ws.work_dir), config=first.config, checks=first.checks)
        result = restored.run('ignored', resume=True, message='Fix add')
        self.assertEqual(result.status, 'verified', result.summary)
        self.assertIn('Fix add', json.dumps(client.seen[0][0]))
        self.assertEqual((self.proj / 'calc.py').read_text(), BUGGY)
        events = [json.loads(x) for x in (first.evidence_dir / 'events.jsonl').read_text().splitlines()]
        self.assertGreater(len(events), before)
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(restored._digest()['user_messages'], ['Fix add'])

    def test_question_during_planning_can_resume_to_approved_plan(self):
        first = self.agent([[('ask_user', {'question': 'What behavior?'})]])
        self.assertEqual(first.run('Fix it').status, 'awaiting_input')
        client = ScriptedClient([[('propose_plan', {'goal': 'fix', 'steps': ['read and edit']})],
            [('read_file', {'path': 'calc.py'})],
            [('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'})],
            [('finish', {'summary': 'Fixed'})]])
        result = Agent(client, Workspace(self.ws.work_dir), config=first.config, checks=first.checks).run(
            '', resume=True, message='It must add')
        self.assertEqual(result.status, 'verified', result.summary)

    def test_invalid_finish_cannot_terminate(self):
        result = self.agent([[('finish', {'surprise': True})],
                             [('respond', {'message': 'No changes'})]], plan_first=False).run('Inspect')
        self.assertEqual(result.status, 'answered')
        self.assertIn('ERROR:', self.client.seen[-1][0][-1]['content'])

    def test_every_native_call_gets_result_even_after_terminal(self):
        agent = self.agent([[('respond', {'message': 'Hello'}),
                             ('write_file', {'path': 'bad.txt', 'content': 'bad'})]], plan_first=False)
        self.assertEqual(agent.run('Hello').status, 'answered')
        self.assertFalse((self.ws.repo / 'bad.txt').exists())
        results = [m for m in agent.messages if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in results], ['c0', 'c1'])

    def test_setup_error_not_masked_by_passing_check(self):
        agent = self.agent([[('write_file', {'path': 'new.py', 'content': 'x=1'})],
            [('finish', {'summary': 'done'})]], plan_first=False, finish_retries=0,
            verify=('syntax', 'tests', 'lint'))
        agent.checks = CheckRunner({'syntax': syntax_check,
            'tests': lambda w: CheckResult('tests', 'passed'),
            'lint': lambda w: CheckResult('lint', 'setup_error')})
        result = agent.run('Add x')
        self.assertEqual(result.status, 'failed_checks')

    def test_live_process_reads_are_not_cached(self):
        agent = self.agent([[('finish', {'summary': ''})]], plan_first=False)
        agent.dev.start('job', [sys.executable, '-c', 'import time; print("started"); time.sleep(.3); print("done")'])
        self.addCleanup(agent.dev.stop_all)
        call = ToolCall('one', 'dev_status', {'name': 'job'})
        first = agent._execute(call, ['dev_status'])
        agent.dev.wait('job', 2)
        second = agent._execute(call, ['dev_status'])
        self.assertIn('running', first)
        self.assertIn('exited(0)', second)
        self.assertNotIn('duplicate', second)

    def test_receipts_written_before_effect_and_unknown_not_replayed(self):
        agent = self.agent([[('finish', {'summary': ''})]], plan_first=False)
        receipt = agent.store.begin('write_file', {'path': 'new.py', 'content': 'x=1'}, 'c0', 'edit')
        agent.run('Inspect')
        client = ScriptedClient([[('write_file', {'path': 'new.py', 'content': 'x=1'})],
                                 [('blocked', {'reason': 'Unknown previous outcome'})]])
        restored = Agent(client, Workspace(self.ws.work_dir), config=agent.config, checks=agent.checks)
        result = restored.run('', resume=True)
        self.assertEqual(result.status, 'blocked')
        self.assertFalse((self.ws.repo / 'new.py').exists())
        self.assertIn('automatic replay is blocked', client.seen[-1][0][-1]['content'])
        row = json.loads((agent.store.operations / (receipt['operation_id'] + '.json')).read_text())
        self.assertEqual(row['status'], 'outcome_unknown')

    def test_no_shell_also_disables_process_launch(self):
        self.assertNotIn('dev_start', build_tools(False, False))
        self.assertNotIn('run_command', build_tools(False, False))

    def test_workspace_create_cannot_destroy_prior_run(self):
        with self.assertRaises(FileExistsError):
            Workspace.create(self.proj, self.ws.work_dir)
        self.assertTrue((self.ws.repo / 'calc.py').exists())

    def test_schema_rejects_boolean_integer_and_nonstring_array(self):
        with self.assertRaises(ToolError):
            build_tools()['read_file'].validate({'path': 'calc.py', 'start': True})
        with self.assertRaises(ToolError):
            build_tools()['dev_start'].validate({'name': 'x', 'argv': [42]})

    def test_resume_does_not_reset_step_budget(self):
        first = self.agent([[('ask_user', {'question': 'Which?'})]], plan_first=False, max_steps=1)
        first.run('Fix')
        client = ScriptedClient([[('respond', {'message': 'should not run'})]])
        result = Agent(client, Workspace(self.ws.work_dir), config=first.config, checks=first.checks).run('', resume=True)
        self.assertEqual(result.status, 'budget_exhausted')
        self.assertEqual(client.seen, [])

    def test_compaction_retains_digest_and_atomic_tool_pairs(self):
        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
        for i in range(30):
            messages.extend([{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': str(i), 'function': {'name': 'read_file', 'arguments': '{"path":"calc.py"}'}}]},
                {'role': 'tool', 'tool_call_id': str(i), 'content': 'x' * 500}])
        compact = bounded_context(messages, {'task': 'fix', 'plan': {'steps': ['test']},
                                             'checks': {'tests': 'failed'}}, 2500)
        self.assertLessEqual(len(json.dumps(compact)), 2500)
        self.assertIn('failed', compact[1]['content'])
        pending = set()
        for message in compact:
            if message.get('tool_calls'):
                pending.update(c['id'] for c in message['tool_calls'])
            elif message['role'] == 'tool':
                self.assertIn(message['tool_call_id'], pending)
                pending.remove(message['tool_call_id'])
        self.assertFalse(pending)

    def test_laptop_profile_http_fix_and_receipts(self):
        from agentharness.__main__ import main
        server = FakeServer('text')
        self.addCleanup(server.close)
        work = self.tmp / 'laptop'
        code = main(['run', str(self.proj), 'Fix add', '--auto-approve', '--profile', 'laptop-i3-12gb',
                     '--base-url', server.url, '--model', 'fake-model', '--work', str(work)])
        self.assertEqual(code, 0)
        for request in server.requests:
            self.assertEqual(request['max_tokens'], 1024)
            self.assertNotIn('tools', request)
            self.assertLessEqual(len(json.dumps(request['messages'], ensure_ascii=False)), 18000)
        self.assertTrue(list((work / 'evidence' / 'operations').glob('*.json')))
        self.assertEqual(json.loads((work / 'evidence' / 'session.json').read_text())['status'], 'verified')


    def test_terminal_conversation_uses_model_without_starting_checks(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentharness.__main__ import main
        client = ScriptedClient(['Hello! What is on your mind?', 'Four.', 'You asked about addition.'])
        with patch('agentharness.__main__._client', return_value=client), \
             patch.object(CheckRunner, 'run') as checks, \
             patch('builtins.input', side_effect=['hey  ', 'What is two plus two?', 'What did I ask?', '/quit']), \
             redirect_stdout(io.StringIO()) as output:
            code = main(['chat', str(self.proj), '--work', str(self.tmp / 'greetings')])
        self.assertEqual(code, 0)
        checks.assert_not_called()
        self.assertEqual(len(client.seen), 3)
        self.assertIn('Four.', output.getvalue())
        self.assertIn('What is two plus two?', json.dumps(client.seen[-1][0]))
        self.assertNotIn('tests: failed', output.getvalue())

    def test_terminal_chat_preserves_followup(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentharness.__main__ import main
        client = ScriptedClient(['Hello!', [('start_work', {'request': 'Explain arithmetic'})],
                                 [('ask_user', {'question': 'Which file?'})],
                                 [('respond', {'message': 'calc.py subtracts'})]])
        with patch('agentharness.__main__._client', return_value=client), \
             patch('builtins.input', side_effect=['hey', 'Explain arithmetic', 'calc.py', '/quit']), \
             redirect_stdout(io.StringIO()) as output:
            code = main(['chat', str(self.proj), '--auto-approve', '--no-plan',
                         '--work', str(self.tmp / 'chat')])
        self.assertEqual(code, 0)
        self.assertIn('calc.py subtracts', output.getvalue())
        self.assertIn('User message: calc.py', json.dumps(client.seen[-1][0]))
        self.assertIn('Explain arithmetic', json.dumps(client.seen[-1][0]))

    def test_greeting_with_task_still_reaches_agent(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentharness.__main__ import main
        client = ScriptedClient([[('start_work', {'request': 'hey, explain calc.py'})],
                                 [('respond', {'message': 'The function subtracts.'})]])
        with patch('agentharness.__main__._client', return_value=client), \
             patch('builtins.input', side_effect=['hey, explain calc.py', '/quit']), \
             redirect_stdout(io.StringIO()) as output:
            main(['chat', str(self.proj), '--no-plan', '--auto-approve',
                  '--work', str(self.tmp / 'greeting-task')])
        self.assertEqual(len(client.seen), 2)
        self.assertIn('hey, explain calc.py', json.dumps(client.seen[0][0]))
        self.assertIn('[model] Waiting for local model', output.getvalue())
        self.assertNotIn('[baseline]', output.getvalue())

    def test_conversation_then_coding_requires_approval_before_baseline(self):
        from unittest.mock import Mock
        client = ScriptedClient(['Hi!', [('start_work', {'request': 'Fix add to return the sum'})],
            [('read_file', {'path': 'calc.py'})],
            [('propose_plan', {'goal': 'Fix add', 'steps': ['Replace subtraction with addition']})],
            [('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'})],
            [('finish', {'summary': 'Fixed add'})], 'You are welcome!'])
        checks = CheckRunner({'syntax': syntax_check, 'tests': UNITTEST})
        checks.run = Mock(wraps=checks.run)
        def approve(plan):
            checks.run.assert_not_called()
            return True, ''
        cfg = AgentConfig(conversational=True)
        first = Agent(client, self.ws, config=cfg, checks=checks, approver=approve)
        self.assertEqual(first.run('hey').status, 'answered')
        checks.run.assert_not_called()
        second = Agent(client, self.ws, config=cfg, checks=checks, approver=approve)
        result = second.run('', resume=True, message='Fix add to return the sum')
        self.assertEqual(result.status, 'verified', result.summary)
        self.assertIn('failed', result.checks['baseline']['tests'])
        self.assertIn('passed', result.checks['final']['tests'])
        checks.run.reset_mock()
        third = Agent(client, self.ws, config=cfg, checks=checks, approver=approve)
        self.assertEqual(third.run('', resume=True, message='Thanks!').status, 'answered')
        checks.run.assert_not_called()
        self.assertEqual((self.proj / 'calc.py').read_text(), BUGGY)

    def test_conversation_rejected_plan_runs_no_checks_or_edits(self):
        from unittest.mock import Mock
        client = ScriptedClient([[('start_work', {'request': 'Fix add'})],
                                [('propose_plan', {'goal': 'Fix', 'steps': ['Edit calc.py']})]])
        checks = CheckRunner({'tests': UNITTEST})
        checks.run = Mock(wraps=checks.run)
        agent = Agent(client, self.ws, config=AgentConfig(conversational=True),
                      checks=checks, approver=lambda plan: (False, ''))
        self.assertEqual(agent.run('Fix add').status, 'rejected')
        checks.run.assert_not_called()
        self.assertEqual((self.ws.repo / 'calc.py').read_text(), BUGGY)

    def test_terminal_chat_recovers_after_model_error(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentharness.__main__ import main
        class RecoveringClient(ScriptedClient):
            def chat(self, messages, tools=None, tool_names=None):
                if not hasattr(self, 'failed'):
                    self.failed = True
                    raise ModelError('temporary connection error')
                return super().chat(messages, tools, tool_names)
        client = RecoveringClient(['I am here.'])
        with patch('agentharness.__main__._client', return_value=client), \
             patch('builtins.input', side_effect=['hello', 'try again', '/quit']), \
             redirect_stdout(io.StringIO()) as output:
            code = main(['chat', str(self.proj), '--work', str(self.tmp / 'recovery')])
        self.assertEqual(code, 0)
        self.assertIn('temporary connection error', output.getvalue())
        self.assertIn('nessa> I am here.', output.getvalue())

    def test_new_work_in_same_chat_needs_new_plan_approval(self):
        client = ScriptedClient([
            [('start_work', {'request': 'Fix add'})],
            [('read_file', {'path': 'calc.py'})],
            [('propose_plan', {'goal': 'Fix add', 'steps': ['Use addition']})],
            [('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'})],
            [('finish', {'summary': 'Fixed'})],
            [('start_work', {'request': 'Now replace add'})],
            [('propose_plan', {'goal': 'Replace add', 'steps': ['Change implementation']})],
        ])
        approvals = []
        def approve(plan):
            approvals.append(plan)
            return len(approvals) == 1, ''
        cfg = AgentConfig(conversational=True)
        checks = CheckRunner({'syntax': syntax_check, 'tests': UNITTEST})
        first = Agent(client, self.ws, config=cfg, checks=checks, approver=approve)
        self.assertEqual(first.run('Fix add').status, 'verified')
        second = Agent(client, self.ws, config=cfg, checks=checks, approver=approve)
        self.assertEqual(second.run('', resume=True, message='Now replace add').status, 'rejected')
        self.assertEqual(len(approvals), 2)
        self.assertIn('return a + b', (self.ws.repo / 'calc.py').read_text())

    def test_chat_cannot_call_edit_tools_before_entering_workflow(self):
        client = ScriptedClient([[('write_file', {'path': 'oops.txt', 'content': 'bad'})], 'Hello.'])
        agent = Agent(client, self.ws, config=AgentConfig(conversational=True, require_approval=False))
        self.assertEqual(agent.run('hello').status, 'answered')
        self.assertFalse((self.ws.repo / 'oops.txt').exists())
        self.assertIn('start_work', client.seen[0][1])
        self.assertIn('weather', client.seen[0][1])
        self.assertNotIn('write_file', client.seen[0][1])
        self.assertIn('run_command', client.seen[0][1])  # separately gated by exact-command approval
        self.assertIn('ERROR', json.dumps(client.seen[-1][0]))

    def test_start_work_requires_a_nonempty_request(self):
        client = ScriptedClient([[('start_work', {'request': ' '})], 'What would you like to do?'])
        agent = Agent(client, self.ws, config=AgentConfig(conversational=True, require_approval=False))
        self.assertEqual(agent.run('hello').status, 'answered')
        self.assertIsNone(agent.plan)
        self.assertFalse(agent.baseline)

    def test_start_work_does_not_replace_user_request_with_model_summary(self):
        client = ScriptedClient([[('start_work', {'request': 'Inspect the file'})],
                                 [('respond', {'message': 'Done inspecting.'})]])
        agent = Agent(client, self.ws, config=AgentConfig(conversational=True, require_approval=False))
        agent.run('Fix add and verify its behavior')
        self.assertEqual(agent.task, 'Fix add and verify its behavior')
        self.assertIn('Task:\nFix add and verify its behavior',
                      '\n'.join(m.get('content', '') for m in client.seen[-1][0]))

    def test_overbudget_calls_are_answered_without_execution(self):
        agent = self.agent([[('write_file', {'path': f'{i}.txt', 'content': 'x'}) for i in range(4)],
                            [('finish', {'summary': 'done'})]], plan_first=False, max_calls_per_turn=2)
        agent.run('Write files')
        self.assertTrue((self.ws.repo / '0.txt').exists())
        self.assertFalse((self.ws.repo / '2.txt').exists())
        first_results = [m for m in self.client.seen[1][0] if m['role'] == 'tool']
        self.assertEqual(len(first_results), 4)

    def test_plan_time_budget_enforced(self):
        agent = self.agent([[('propose_plan', {'goal': 'g', 'steps': ['s']})]], time_budget=-1)
        self.assertEqual(agent.run('Fix').status, 'budget_exhausted')
        self.assertEqual(self.client.seen, [])

    def test_http_400_schema_error_not_treated_as_context_overflow(self):
        import io
        import urllib.error
        from unittest.mock import patch
        client = ChatClient('http://localhost:11434/v1', 'm')
        error = urllib.error.HTTPError('http://localhost', 400, 'bad', {},
                                      io.BytesIO(b'{"error":"tools are not supported"}'))
        with patch.object(client._opener, 'open', side_effect=error):
            with self.assertRaises(ModelError) as caught:
                client.chat([])
        from agentharness.llm import ContextOverflow
        self.assertNotIsInstance(caught.exception, ContextOverflow)

    def test_native_missing_id_normalized_for_request_and_result(self):
        from unittest.mock import patch
        client = ChatClient('http://localhost:11434/v1', 'm')
        response = {'choices': [{'message': {'tool_calls': [{'function': {
            'name': 'read_file', 'arguments': {'path': 'calc.py'}}}]}}]}
        with patch.object(client, '_request', return_value=response):
            reply = client.chat([])
        self.assertEqual(reply.tool_calls[0].id, reply.raw_tool_calls[0]['id'])
        self.assertIsInstance(reply.raw_tool_calls[0]['function']['arguments'], str)

    def test_command_mutation_is_checkpointed_even_when_command_fails(self):
        agent = self.agent([[('finish', {'summary': ''})]], plan_first=False)
        command = f'"{sys.executable}" -c "from pathlib import Path; Path(\'new.py\').write_text(\'x=1\'); raise SystemExit(1)"'
        output = agent._execute(ToolCall('c', 'run_command', {'command': command}), ['run_command'])
        self.assertTrue(output.startswith('exit=1'))
        self.assertEqual(agent.edit_count, 1)
        self.assertTrue(list((agent.evidence_dir / 'checkpoints').glob('*.diff')))

    def test_restart_closes_only_current_envelope_with_reused_ids(self):
        agent = self.agent([[('respond', {'message': 'Hi'})]], plan_first=False)
        agent.run('Hi')
        state = agent.store.load()
        state['messages'].append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'c0', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"calc.py"}'}}]})
        agent.store.save(state)
        client = ScriptedClient([[('respond', {'message': 'resumed'})]])
        result = Agent(client, Workspace(self.ws.work_dir), config=agent.config, checks=agent.checks).run('', resume=True)
        self.assertEqual(result.status, 'answered')
        messages = client.seen[0][0]
        envelope = max(i for i, m in enumerate(messages) if m.get('tool_calls'))
        self.assertEqual(messages[envelope + 1]['role'], 'tool')
        self.assertIn('Outcome unknown', messages[envelope + 1]['content'])


if __name__ == '__main__':
    unittest.main()
