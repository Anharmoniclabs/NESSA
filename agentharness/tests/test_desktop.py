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
