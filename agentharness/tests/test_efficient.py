import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentharness.agent import Agent, AgentConfig
from agentharness import efficient
from agentharness.desktop import App
from agentharness.llm import Reply
from agentharness.memory import LessonStore
from agentharness.tests.test_agent import Base, ScriptedClient


class EfficientChat(Base):
    def test_ordinary_chat_uses_small_prompt_without_old_assistant_loop(self):
        client = ScriptedClient(['An LLM uses attention.'])
        cfg = AgentConfig(conversational=True, efficient_chat=True, require_approval=False)
        Agent(client, self.ws, config=cfg, chat_client=client,
              conversation_context='user: Explain matrix multiplication.').run('How does it work?')
        messages, schemas = client.seen[0]
        self.assertLess(len(json.dumps(messages)) + len(json.dumps(schemas)), 4000)
        self.assertEqual(schemas, [])
        self.assertIn('matrix multiplication', json.dumps(messages))

    def test_tools_remain_available_for_actual_requests(self):
        available = ['start_work', 'weather', 'web_search', 'web_fetch', 'news_search', 'read_file', 'run_command', 'studio_control']
        cases = [('weather tomorrow', 'weather'), ('latest news', 'news_search'),
                 ('read the file', 'read_file'), ('run pwd', 'run_command'), ('make a music beat', 'studio_control')]
        for request, tool in cases:
            self.assertIn(tool, efficient.chat_tools(request, available))
        self.assertIn('weather', efficient.chat_tools('And tomorrow?', available, 'weather in Boston'))

    def test_restarted_chat_replaces_old_generated_identity_with_retrieved_history(self):
        cfg = AgentConfig(conversational=True, efficient_chat=True, require_approval=False)
        first = ScriptedClient(['I am a human.'])
        Agent(first, self.ws, config=cfg, chat_client=first).run('Who are you?')
        client = ScriptedClient(['I am a local AI model.'])
        Agent(client, self.ws, config=cfg, chat_client=client,
              conversation_context='user correction: you are not human.').run('', resume=True, message='Explain what you are.')
        prompt = json.dumps(client.seen[0][0])
        self.assertNotIn('I am a human.', prompt)
        self.assertIn('you are not human', prompt)
        self.assertIn('Explain what you are.', prompt)


class LocalMemory(unittest.TestCase):
    def test_evidence_truncation_preserves_valid_json_and_failure_tail(self):
        result = efficient.bounded_evidence('开始' * 3000 + '\nFAILED: expected 3, got 4', 900)
        self.assertLessEqual(len(result.encode()), 900)
        self.assertIn('FAILED: expected 3, got 4', json.loads(result)['output_tail'])
        self.assertTrue(json.loads(result)['truncated'])

    def test_default_client_rejects_cloud_even_through_loopback(self):
        from agentharness.llm import ChatClient
        with self.assertRaisesRegex(ValueError, 'Cloud model'):
            ChatClient('http://127.0.0.1:11435/v1', 'nemotron-3-super:cloud')

    def test_corrections_supersede_without_erasing_history_and_forget_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lessons.jsonl'
            store = LessonStore(path)
            store.add('*', 'Keep replies short.', key='style')
            row = store.add('*', 'Explain in detail.', key='style')
            self.assertEqual(row['version'], 2)
            self.assertEqual(store.relevant('*', 'style'), ['Explain in detail.'])
            self.assertEqual(len(store.all('*', history=True)), 2)
            store.add('/other', 'Other project detail.', key='style')
            store.forget('*', 'style')
            self.assertEqual(LessonStore(path).relevant('*', 'style'), [])
            self.assertEqual(len(store.all('*', history=True)), 3)

    def test_unicode_notes_obey_byte_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LessonStore(Path(directory) / 'notes')
            for i in range(4):
                store.add('*', 'こんにちは' * 80, key=str(i))
            self.assertLessEqual(sum(len(note.encode()) for note in store.relevant('*', 'hello')), 1600)

    def test_memory_command_never_calls_model_and_is_used_by_next_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root / 'chats', 'http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
            key = app.create()['id']
            chat = app.chats[key]
            store = LessonStore(root / 'lessons.jsonl')
            with patch('agentharness.desktop.LessonStore', return_value=store), patch('agentharness.desktop.ChatClient') as client:
                app.run(chat, 'remember response_style: Explain directly without an introduction.')
                client.assert_not_called()
                client.return_value.model = 'nessa-lfm:latest'
                client.return_value.base_url = app.base_url
                client.return_value.chat.return_value = Reply('Hello.', [], False)
                app.run(chat, 'Hello')
                self.assertIn('Explain directly', str(client.return_value.chat.call_args))
                app.run(chat, 'forget response_style')
                self.assertEqual(store.relevant('*', 'style'), [])
