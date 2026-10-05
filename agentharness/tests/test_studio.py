import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentharness import studio
from agentharness.desktop import App
from agentharness.tools import build_tools
from agentharness.agent import Agent, AgentConfig
from agentharness.tests.test_agent import Base, ScriptedClient


class StudioTests(unittest.TestCase):
    def test_explicit_beat_requests_and_negation(self):
        self.assertEqual(studio.quick_request('can you go into my anharmonicstudio and make a beat?'),
                         {'action': 'make_beat', 'kit': 'Pocket'})
        self.assertEqual(studio.quick_request('make a trap beat at 140 bpm')['bpm'], 140)
        for text in ('do not make a beat', 'explain how to make a beat',
                     'fix the code to make a beat', 'make a beat and delete my song',
                     'she said "make a beat"', 'play it'):
            self.assertIsNone(studio.quick_request(text))
        self.assertEqual(studio.quick_request('play it', studio_context=True), {'action': 'play'})

    def test_desktop_production_bypasses_model_and_project_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp), 'unused', 'unused')
            key = app.create()['id']
            result = json.dumps(dict(ok=True, action='make_beat', pattern='Pocket groove', bpm=120))
            with patch('agentharness.studio.command', return_value=result) as command, \
                 patch('agentharness.desktop.ChatClient') as model, \
                 patch('agentharness.desktop.find_project') as discovery:
                app.run(app.chats[key], 'can you go into my anharmonicstudio and make a beat?')
            command.assert_called_once()
            model.assert_not_called()
            discovery.assert_not_called()
            self.assertTrue(app.snapshot(key)['studio_connected'])
            self.assertIn('Created Pocket groove', app.snapshot(key)['messages'][-1]['content'])
            receipt = next((Path(tmp) / key / 'evidence/operations').glob('*.json'))
            self.assertEqual(json.loads(receipt.read_text())['status'], 'completed')

    def test_unknown_outcome_does_not_launch_or_replay(self):
        with patch('agentharness.studio._connect') as connect, \
             patch('agentharness.studio.subprocess.Popen') as launch:
            sock = connect.return_value
            sock.recv.side_effect = socket.timeout('timeout')
            result = studio.command({'action': 'make_beat'})
        self.assertTrue(result.startswith('TIMEOUT'))
        launch.assert_not_called()
        sock.sendall.assert_called_once()

    def test_tool_has_control_kind_and_schema(self):
        tool = build_tools()['studio_control']
        self.assertEqual(tool.kind, 'control')
        self.assertFalse(tool.cacheable)
        self.assertIn('make_beat', tool.schema()['function']['parameters']['properties']['action']['enum'])


class StudioModelRoutingTests(Base):
    def test_model_can_use_studio_control_in_chat_with_receipt(self):
        client = ScriptedClient([[('studio_control', {'action': 'make_beat', 'kit': 'Midnight'})],
                                 'Created the beat.'])
        output = json.dumps(dict(ok=True, action='make_beat', pattern='Midnight groove', bpm=120))
        with patch('agentharness.studio.command', return_value=output) as command:
            agent = Agent(client, self.ws, chat_client=client,
                          config=AgentConfig(conversational=True, require_approval=False))
            result = agent.run('Give me a Midnight drum groove in AnharmonicStudio')
        command.assert_called_once_with({'action': 'make_beat', 'kit': 'Midnight'})
        self.assertEqual(result.status, 'answered')
        self.assertIn('studio_control', client.seen[0][1])
        self.assertIn('Midnight groove', json.dumps(client.seen[-1][0]))
        receipts = [json.loads(p.read_text()) for p in agent.store.operations.glob('*.json')]
        self.assertTrue(any(r['tool_name'] == 'studio_control' and r['status'] == 'completed' for r in receipts))

    def test_unknown_outcome_is_not_replayed_by_model(self):
        call = [('studio_control', {'action': 'make_beat'})]
        client = ScriptedClient([call, call, 'Inspect Studio before retrying.'])
        with patch('agentharness.studio.command', return_value='TIMEOUT: outcome unknown') as command:
            agent = Agent(client, self.ws, chat_client=client,
                          config=AgentConfig(conversational=True, require_approval=False))
            agent.run('Make a beat')
        command.assert_called_once()
