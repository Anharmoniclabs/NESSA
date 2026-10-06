import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentharness.agent import RunResult
from agentharness.desktop import App
from agentharness.llm import Reply


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'calc.py').write_text('value = 1\n')
        self.app = App(self.root / 'sessions', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
        self.key = self.app.create(str(self.project))['id']

    def wait(self, predicate):
        end = time.monotonic()+3
        while not predicate() and time.monotonic()<end:
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_snapshot_and_restart_preserve_history(self):
        chat = self.app.chats[self.key]
        chat.data['messages'].append({'role':'user','content':'hello'})
        chat.data['busy'] = True
        chat.save()
        restored = App(self.root / 'sessions', self.app.base_url, self.app.model)
        self.assertFalse(restored.snapshot(self.key)['busy'])
        self.assertEqual(restored.snapshot(self.key)['messages'][0]['content'], 'hello')
        copy = restored.snapshot(self.key)
        copy['messages'].clear()
        self.assertTrue(restored.snapshot(self.key)['messages'])

    def test_multipart_replies_are_shown_once_and_checkpointed(self):
        key = self.app.create()['id']
        with patch('agentharness.desktop.ChatClient') as client:
            client.return_value.model = 'test'
            client.return_value.base_url = self.app.base_url
            client.return_value.chat.side_effect = [
                Reply('The castle. [[CONTINUE]]', [], False, finish_reason='stop'),
                Reply('The rescue.', [], False, finish_reason='stop')]
            self.app.send(key, 'Outline a romance')
            self.wait(lambda: not self.app.snapshot(key)['busy'])
        data = self.app.snapshot(key)
        self.assertEqual([m['content'] for m in data['messages'][1:]], ['The castle.', 'The rescue.'])
        self.assertEqual(data['result']['status'], 'answered')
        restored = App(self.app.root, self.app.base_url, self.app.model)
        self.assertEqual(restored.snapshot(key)['messages'], data['messages'])

    def test_crash_restores_stream_to_model_history_once(self):
        key = self.app.create()['id']
        chat = self.app.chats[key]
        evidence = chat.directory / 'evidence'
        evidence.mkdir()
        state = dict(phase='chat', task='Knight romance', messages=[dict(role='user', content='Knight romance')])
        (evidence / 'session.json').write_text(json.dumps(state))
        chat.data.update(busy=True, stream_message_count=1, partial='Rowan reached the island.')
        chat.save()
        restored = App(self.app.root, self.app.base_url, self.app.model)
        saved = json.loads((evidence / 'session.json').read_text())
        self.assertEqual(saved['messages'][-1]['content'], 'Rowan reached the island.')
        self.assertTrue(saved['chat_progress']['pending'])
        again = App(self.app.root, self.app.base_url, self.app.model)
        self.assertEqual(again.snapshot(key)['messages'], restored.snapshot(key)['messages'])

    def test_delta_is_durable_before_completion(self):
        chat = self.app.chats[self.key]
        chat.event('model_started', dict(model='test', message_count=2))
        chat.delta('A saved opening')
        saved = json.loads((chat.directory / 'chat.json').read_text())
        self.assertEqual(saved['partial'], 'A saved opening')

    def test_named_project_inspection_does_not_wait_for_model(self):
        project = self.root / 'Projects' / 'AnharmonicStudio'
        project.mkdir(parents=True)
        (project / 'README.md').write_text('Studio source')
        key = self.app.create()['id']
        with patch('agentharness.desktop.Path.home', return_value=self.root), \
                patch('agentharness.desktop.ChatClient') as model:
            self.app.send(key, 'can we inspect the anharmoinic studio app in my files')
            self.wait(lambda: not self.app.snapshot(key)['busy'])
            model.assert_not_called()
        result = self.app.snapshot(key)
        self.assertEqual(result['project'], str(project))
        self.assertIn('README.md', result['messages'][-1]['content'])
        self.assertFalse((self.app.chats[key].directory / 'project-work').exists())

    def test_worker_approval_and_private_copy(self):
        class FakeAgent:
            def __init__(inner, client, ws, **kw):
                inner.ws, inner.kw = ws, kw
            def run(inner, task, **kw):
                approved, _ = inner.kw['approver']({'goal':'Change value','steps':['Edit calc.py']})
                if approved:
                    (inner.ws.repo / 'calc.py').write_text('value = 2\n')
                return RunResult('unverified' if approved else 'rejected', 'Done', inner.ws.patch(), 1, [], None)
        with patch('agentharness.desktop.Agent', FakeAgent):
            self.app.send(self.key, 'Change value')
            self.wait(lambda:self.app.snapshot(self.key)['plan'] is not None)
            with self.assertRaises(ValueError):
                self.app.send(self.key,'Overlapping message')
            self.assertEqual((self.project/'calc.py').read_text(),'value = 1\n')
            self.app.decide(self.key, True)
            self.wait(lambda:not self.app.snapshot(self.key)['busy'])
        self.assertEqual((self.project/'calc.py').read_text(),'value = 1\n')
        self.assertIn('value = 2',self.app.snapshot(self.key)['result']['patch'])

    def test_cancel_waiting_approval_finishes_worker(self):
        class FakeAgent:
            def __init__(inner, client, ws, **kw): inner.kw=kw
            def run(inner, *args, **kw): inner.kw['approver']({'goal':'Wait'})
        with patch('agentharness.desktop.Agent', FakeAgent):
            self.app.send(self.key,'Work')
            self.wait(lambda:self.app.snapshot(self.key)['plan'] is not None)
            self.app.stop(self.key)
            self.wait(lambda:not self.app.snapshot(self.key)['busy'])
        self.assertEqual(self.app.snapshot(self.key)['messages'][-1]['status'],'cancelled')

    def test_invalid_project_and_empty_message(self):
        with self.assertRaises(FileNotFoundError): self.app.create(str(self.root/'missing'))
        with self.assertRaises(ValueError): self.app.send(self.key,' ')

    def test_chat_without_project_answers_and_resumes(self):
        key = self.app.create()['id']
        chat = self.app.chats[key]
        self.assertIsNone(chat.data['project'])
        self.assertEqual(list((chat.directory / 'repo').iterdir()), [])
        with patch('agentharness.desktop.ChatClient') as client:
            client.return_value.model = 'test'
            client.return_value.base_url = self.app.base_url
            client.return_value.chat.return_value = Reply('Hello, Alex.', [], False)
            self.app.send(key, 'Hello, my name is Alex.')
            self.wait(lambda:not self.app.snapshot(key)['busy'])
            self.assertEqual(self.app.snapshot(key)['result']['status'], 'answered')
            self.assertEqual(self.app.snapshot(key)['result']['changed_files'], [])
            self.assertFalse(any(self.app.snapshot(key)['result']['checks'].values()))
            restored = App(self.root / 'sessions', self.app.base_url, self.app.model)
            client.return_value.chat.return_value = Reply('Alex.', [], False)
            restored.send(key, 'What is my name?')
            self.wait(lambda:not restored.snapshot(key)['busy'])
            self.assertEqual(restored.snapshot(key)['messages'][-1]['content'], 'Alex.')
            self.assertIn('my name is Alex', str(client.return_value.chat.call_args))

    def test_visible_history_and_global_lessons_reach_model_without_agent_session(self):
        key = self.app.create()['id']
        chat = self.app.chats[key]
        chat.data['messages'] = [
            dict(role='user', content='Remember: my song is called Blue Moon.'),
            dict(role='assistant', content='Studio opened.')]
        chat.save()
        with patch('agentharness.desktop.ChatClient') as client, \
                patch('agentharness.desktop.LessonStore') as lessons:
            lessons.return_value.relevant.return_value = ['Use plain language.']
            client.return_value.model = 'test'
            client.return_value.base_url = self.app.base_url
            client.return_value.chat.return_value = Reply('Blue Moon.', [], False)
            self.app.send(key, 'What did I call my song?')
            self.wait(lambda: not self.app.snapshot(key)['busy'])
            prompt = str(client.return_value.chat.call_args)
            self.assertIn('Blue Moon', prompt)
            self.assertIn('Use plain language.', prompt)
            self.assertIn('not a human', prompt)
        self.assertTrue((chat.directory / 'memory-cache.json').exists())
        self.assertTrue(any(e['event'] == 'memory_loaded' for e in chat.data['activity']))


if __name__ == '__main__': unittest.main()


class ApplyChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'calc.py').write_text('value = 1\n')
        self.app = App(self.root / 'sessions', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
        self.key = self.app.create(str(self.project))['id']
        chat = self.app.chats[self.key]
        (chat.directory / 'repo' / 'calc.py').write_text('value = 2\n')
        (chat.directory / 'repo' / 'new.py').write_text('x = 1\n')
        chat.data['result'] = dict(status='verified', patch='diff', changed_files=['calc.py', 'new.py'])

    def test_apply_writes_changes_and_rebases(self):
        self.assertEqual(self.app.apply(self.key), ['calc.py', 'new.py'])
        self.assertEqual((self.project / 'calc.py').read_text(), 'value = 2\n')
        self.assertEqual((self.project / 'new.py').read_text(), 'x = 1\n')
        with self.assertRaisesRegex(ValueError, 'no unapplied'):
            self.app.apply(self.key)

    def test_apply_refuses_conflicts_without_writing(self):
        (self.project / 'calc.py').write_text('value = 99  # user edit\n')
        with self.assertRaisesRegex(ValueError, 'Conflicts: calc.py'):
            self.app.apply(self.key)
        self.assertEqual((self.project / 'calc.py').read_text(), 'value = 99  # user edit\n')
        self.assertFalse((self.project / 'new.py').exists())

    def test_apply_requires_accepted_result(self):
        self.app.chats[self.key].data['result']['status'] = 'failed_checks'
        with self.assertRaisesRegex(ValueError, 'failed_checks'):
            self.app.apply(self.key)
        self.assertEqual((self.project / 'calc.py').read_text(), 'value = 1\n')

    def test_chat_command_applies(self):
        self.app.send(self.key, 'apply changes')
        end = time.monotonic() + 3
        while self.app.snapshot(self.key)['busy'] and time.monotonic() < end:
            time.sleep(.01)
        self.assertIn('Applied to', self.app.snapshot(self.key)['messages'][-1]['content'])
        self.assertEqual((self.project / 'calc.py').read_text(), 'value = 2\n')


class DesktopEnterpriseTests(unittest.TestCase):
    wait = DesktopTests.wait

    def setUp(self):
        DesktopTests.setUp(self)
        self.app = App(self.root / 'enterprise', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest',
                       profile='lfm-32k', enterprise=True)
        self.key = self.app.create(str(self.project))['id']

    def test_project_chat_edits_in_place_and_apply_is_refused(self):
        chat = self.app.chats[self.key]
        self.assertTrue((chat.directory / 'DIRECT').exists())
        self.assertFalse((chat.directory / 'repo').exists())
        chat.data['result'] = dict(status='completed')
        with self.assertRaisesRegex(ValueError, 'already in the project'):
            self.app.apply(self.key)

    def test_permission_prompt_uses_approval_panel(self):
        import threading
        chat = self.app.chats[self.key]
        answers = []
        worker = threading.Thread(target=lambda: answers.append(chat.permit('run_command', {'command': 'ls'})))
        worker.start()
        self.wait(lambda: self.app.snapshot(self.key)['plan'] is not None)
        self.assertEqual(self.app.snapshot(self.key)['plan']['goal'], 'Allow run_command?')
        self.app.decide(self.key, True)
        worker.join(3)
        self.assertEqual(answers, [(True, '', False)])

    def test_chat_without_project_writes_visible_scratch_files(self):
        seen = {}

        class FakeAgent:
            def __init__(inner, client, ws, **kw):
                inner.ws, inner.kw = ws, kw

            def run(inner, task, **kw):
                seen.update(direct=inner.ws.direct, mode=inner.kw['config'].permission_mode,
                            plan_first=inner.kw['config'].plan_first)
                (inner.ws.repo / 'game.py').write_text('print("hi")\n')
                return RunResult('completed', 'Wrote game.py', '', 1, ['game.py'], None)
        self.app.scratch_root = self.root / 'scratch'
        key = self.app.create()['id']
        with patch('agentharness.desktop.Agent', FakeAgent):
            self.app.send(key, 'write a python game super simple and launch')
            self.wait(lambda: not self.app.snapshot(key)['busy'])
        self.assertEqual(seen, dict(direct=True, mode='default', plan_first=False))
        games = list((self.root / 'scratch').rglob('game.py'))
        self.assertEqual(len(games), 1)
        self.assertFalse((self.app.chats[key].directory / 'repo').exists())


class ModelPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def app(self, cloud):
        catalog = [dict(id='zai-org/GLM-5.3', context=1048576, providers=['novita']),
                   dict(id='Qwen/Qwen3-Coder-480B-A35B-Instruct', context=262144, providers=['novita'])]
        patcher = patch('agentharness.cloud.catalog', return_value=catalog)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch('agentharness.cloud.load_token', return_value='hf_test'):
            return App(self.root / 'sessions', 'http://127.0.0.1:11435/v1', 'nessa-lfm-32k:latest',
                       profile='lfm-32k', cloud=cloud)

    def test_options_list_auto_cloud_and_installed_local_models(self):
        app = self.app(cloud=True)
        with patch('agentharness.desktop.ChatClient.models', return_value=['nessa-lfm-32k:latest', 'qwen2.5-coder:3b']):
            options = app.model_options()
        choices = [c for _, c in options]
        self.assertEqual(choices[0], 'auto')
        self.assertIn('cloud:zai-org/GLM-5.3', choices)
        self.assertIn('local:qwen2.5-coder:3b', choices)
        self.assertIn('cloud:Qwen/Qwen3-Coder-480B-A35B-Instruct', choices)  # every router model, not just 3
        self.assertEqual(choices.count('cloud:zai-org/GLM-5.3'), 1)
        self.assertEqual(app.model_choice, 'auto')

    def test_choice_persists_and_local_choice_uses_only_that_model(self):
        app = self.app(cloud=True)
        app.set_model('local:qwen2.5-coder:3b')
        again = self.app(cloud=True)
        self.assertEqual(again.model_choice, 'local:qwen2.5-coder:3b')
        client, fast = again._clients({'max_tokens': 100, 'temperature': 0, 'reasoning_effort': None}, None, None)
        self.assertEqual((client.model, fast.model), ('qwen2.5-coder:3b', 'qwen2.5-coder:3b'))

    def test_specific_cloud_model_has_no_silent_fallback(self):
        app = self.app(cloud=True)
        app.set_model('cloud:moonshotai/Kimi-K3')
        client, fast = app._clients({'max_tokens': 100, 'temperature': 0, 'reasoning_effort': None}, None, None)
        self.assertEqual([c.model for c in client.clients], ['moonshotai/Kimi-K3'])
        self.assertEqual([c.model for c in fast.clients], ['moonshotai/Kimi-K3'])

    def test_failures_are_labelled_in_the_picker(self):
        app = self.app(cloud=True)
        app._note_failures(['zai-org/GLM-5.3: HTTP 402: You have depleted your monthly included credits'])
        with patch('agentharness.desktop.ChatClient.models', return_value=[]):
            labels = [label for label, _ in app.model_options()]
        self.assertIn('★ GLM-5.3 · cloud · 1048K (no credits)', labels)

    def test_cloud_choices_refused_when_cloud_is_off(self):
        app = self.app(cloud=False)
        self.assertEqual(app.model_choice, 'local:nessa-lfm-32k:latest')
        with self.assertRaises(ValueError):
            app.set_model('auto')

    def test_auto_skips_models_known_to_be_out_of_credits(self):
        app = self.app(cloud=True)
        app._note_failures(['zai-org/GLM-5.3: HTTP 402: depleted your monthly included credits'])
        client, _ = app._clients({'max_tokens': 100, 'temperature': 0, 'reasoning_effort': None}, None, None)
        self.assertNotIn('zai-org/GLM-5.3', [c.model for c in client.clients])
        for model in ('moonshotai/Kimi-K3', 'deepseek-ai/DeepSeek-V4-Pro-0813'):
            app._note_failures([model + ': HTTP 402: credits'])
        client, fast = app._clients({'max_tokens': 100, 'temperature': 0, 'reasoning_effort': None}, None, None)
        self.assertEqual(client.model, 'nessa-lfm-32k:latest')  # straight to on-device, no cloud round trips


class AllowAllTests(unittest.TestCase):
    wait = DesktopTests.wait

    def setUp(self):
        DesktopTests.setUp(self)
        self.app = App(self.root / 'aa', 'http://127.0.0.1:11435/v1', 'nessa-lfm-32k:latest', enterprise=True)
        self.key = self.app.create(str(self.project))['id']

    def test_allow_all_approves_this_and_every_later_action(self):
        import threading
        chat = self.app.chats[self.key]
        answers = []
        worker = threading.Thread(target=lambda: answers.append(chat.permit('write_file', {'path': 'a.py'})))
        worker.start()
        self.wait(lambda: self.app.snapshot(self.key)['plan'] is not None)
        self.app.decide(self.key, True, allow_all=True)
        worker.join(3)
        self.assertEqual(answers, [(True, '', False)])
        self.assertEqual(chat.permit('dev_start', {'name': 'game'}), (True, '', False))  # no prompt
        self.assertIsNone(self.app.snapshot(self.key)['plan'])
