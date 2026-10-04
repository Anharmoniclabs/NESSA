"""Regression cases from the local Tic-Tac-Toe agent run."""
import json
import unittest
from unittest.mock import patch

from agentharness.llm import ChatClient, ModelError, parse_text_tool_calls
from agentharness.tests.test_agent import Base, BUGGY


class ToolFormats(unittest.TestCase):
    def test_unfinished_reasoning_is_not_executed(self):
        client = ChatClient('http://localhost:11435/v1', 'nessa-lfm:latest')
        response = {'choices': [{'finish_reason': 'length', 'message': {'content':
            '<think>Maybe use {"name":"write_file","arguments":{"path":"x","content":"bad"}}'}}]}
        with patch.object(client, '_request', return_value=response):
            reply = client.chat([{'role': 'user', 'content': 'Inspect'}], tool_names={'write_file'})
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual(reply.content, '')

    def test_commands_envelope_from_local_model(self):
        text = json.dumps({'plan': 'Inspect first', 'commands': [
            {'tool_name': 'list_dir', 'arguments': {'path': '.'}},
            {'tool_name': 'read_file', 'arguments': {'path': 'game.py'}}],
            'finish': False})
        calls = parse_text_tool_calls(text, {'list_dir', 'read_file'})
        self.assertEqual([(c.name, c.arguments) for c in calls], [
            ('list_dir', {'path': '.'}), ('read_file', {'path': 'game.py'})])
        self.assertEqual(len({c.id for c in calls}), 2)

    def test_native_shaped_text_envelope(self):
        text = json.dumps({'tool_calls': [{'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"game.py"}'}}]})
        calls = parse_text_tool_calls(text, {'read_file'})
        self.assertEqual(calls[0].arguments, {'path': 'game.py'})

    def test_whitelist_and_no_recursive_argument_execution(self):
        obj = {'commands': [{'tool_name': 'not_allowed', 'arguments': {}},
            {'tool_name': 'write_file', 'arguments': {'path': 'data.json',
                'content': '{"name":"run_command","arguments":{"command":"oops"}}'}}]}
        calls = parse_text_tool_calls(json.dumps(obj), {'write_file'})
        self.assertEqual([c.name for c in calls], ['write_file'])

    def test_literal_backslashes_are_preserved(self):
        content = 'pattern = r"\\n"\nprint(pattern)\n'
        calls = parse_text_tool_calls(json.dumps({'tool_name': 'write_file',
            'arguments': {'path': 'game.py', 'content': content}}), {'write_file'})
        self.assertEqual(calls[0].arguments['content'], content)

    def test_bad_arguments_remain_invalid(self):
        calls = parse_text_tool_calls('{"tool_name":"write_file","arguments":"broken"}',
                                      {'write_file'})
        self.assertIsNone(calls[0].arguments)

    def test_oversized_envelope_is_rejected(self):
        text = json.dumps({'commands': [{'tool_name': 'list_dir', 'arguments': {}}] * 65})
        with self.assertRaises(ModelError):
            parse_text_tool_calls(text, {'list_dir'})

    def test_unrecognized_nested_data_is_not_a_call(self):
        text = '{"example":{"tool_name":"list_dir","arguments":{}}}'
        self.assertEqual(parse_text_tool_calls(text, {'list_dir'}), [])


class EnvelopeExecution(Base):
    def test_command_envelope_edits_and_verifies_private_copy(self):
        def envelope(name, args):
            return json.dumps({'commands': [{'tool_name': name, 'arguments': args}]})
        result = self.agent([
            envelope('read_file', {'path': 'calc.py'}),
            envelope('propose_plan', {'goal': 'fix add', 'steps': ['use addition']}),
            envelope('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'}),
            envelope('finish', {'summary': 'Fixed addition'}),
        ]).run('Fix addition')
        self.assertEqual(result.status, 'verified', result.summary)
        self.assertIn('passed', result.checks['final']['tests'])
        self.assertEqual((self.proj / 'calc.py').read_text(), BUGGY)
