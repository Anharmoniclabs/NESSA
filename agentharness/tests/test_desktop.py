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


if __name__ == '__main__': unittest.main()
