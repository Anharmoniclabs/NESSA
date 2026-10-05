import json
import tempfile
import unittest
from pathlib import Path

from agentharness.memory import ConversationMemory


class ConversationMemoryTests(unittest.TestCase):
    def test_full_log_survives_while_prompt_is_bounded_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memory-cache.json'
            messages = [dict(role='user', content='Remember: stop repeating your introduction.')]
            messages += [dict(role='assistant', content='I am a human.')] * 80
            messages += [dict(role='user', content='Explain matrix multiplication.')]
            context = ConversationMemory(path).refresh(messages, 'Explain matrix multiplication.', 1200)
            self.assertLessEqual(len(context), 1200)
            self.assertIn('stop repeating', context)
            self.assertEqual(context.count('I am a human.'), 0)
            self.assertEqual(json.loads(path.read_text())['messages'][-1]['content'], messages[-1]['content'])
            self.assertEqual(len(json.loads(path.read_text())['messages']), 82)
            restored = ConversationMemory(path).refresh(messages, 'Explain matrix multiplication.', 1200)
            self.assertEqual(context, restored)

    def test_middle_of_long_history_is_retrievable_and_deleted_turns_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ConversationMemory(Path(directory) / 'memory-cache.json')
            messages = [dict(role='user', content='Talk about weather ' + str(i)) for i in range(90)]
            messages[40]['content'] = 'The submarine is called Marigold.'
            context = cache.refresh(messages, 'What is the submarine called?')
            self.assertIn('Marigold', context)
            self.assertNotIn('Marigold', cache.refresh([], 'submarine'))
            self.assertEqual(json.loads(cache.path.read_text())['messages'], [])
