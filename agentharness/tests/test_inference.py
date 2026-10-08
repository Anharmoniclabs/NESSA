"""Accounting boundaries and isolated batch routing state."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agentharness.llm import ChatClient
from agentharness.batch import run_batch
from agentharness.tests.test_fast_chat import stream, chunk


class Accounting(unittest.TestCase):
    def test_stream_separates_reasoning_visible_and_usage(self):
        client = ChatClient('http://localhost/v1', 'requested', stream=True)
        response = stream(chunk({'role': 'assistant'}), chunk({'reasoning_content': 'thinking'}),
                          chunk({'content': 'answer'}, 'stop'),
                          {'model': 'served', 'choices': [], 'usage': {'prompt_tokens': 100,
                           'completion_tokens': 11, 'prompt_tokens_details': {'cached_tokens': 64},
                           'completion_tokens_details': {'reasoning_tokens': 4}}})
        with patch.object(client._opener, 'open', return_value=response), \
             patch('agentharness.llm.time.monotonic', side_effect=range(20)):
            reply = client.chat([])
        m = reply.inference
        self.assertEqual(m['ttft_seconds'], 3)  # role-only and blank SSE lines ignored
        self.assertEqual(m['first_visible_seconds'], 5)
        self.assertEqual(m['generation_span_seconds'], 2)
        self.assertEqual(m['decode_tokens_per_second_estimate'], 5)
        self.assertEqual(m['cached_tokens'], 64)
        self.assertEqual(m['served_model'], 'served')

    def test_nonstream_missing_usage_stays_unknown(self):
        client = ChatClient('http://localhost/v1', 'm')
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': 'ok'}}]}):
            m = client.chat([]).inference
        for name in ('cached_tokens', 'completion_tokens', 'ttft_seconds', 'decode_tokens_per_second_estimate'):
            self.assertIsNone(m[name])

    def test_parallel_batch_owns_one_client_per_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'tasks.jsonl').write_text('\n'.join(json.dumps({'id': str(i)}) for i in range(2)))
            clients = []
            barrier = threading.Barrier(2)
            def factory():
                client = object()
                clients.append(client)
                return client
            def solve(client, comp, record, runs, config):
                barrier.wait(timeout=5)
                return dict(task_id=record['id'], patch='', status='unverified', steps=0)
            with patch('agentharness.batch.solve_one', side_effect=solve):
                run_batch(None, root, root/'out.jsonl', root/'runs', workers=2, client_factory=factory)
            self.assertEqual(len(clients), 2)
            self.assertIsNot(clients[0], clients[1])
