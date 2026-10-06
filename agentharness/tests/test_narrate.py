"""Activity narration: real events only, model reasoning surfaced, current turn isolated."""
import unittest

from agentharness import narrate


class Narration(unittest.TestCase):
    def test_tool_calls_become_plain_english(self):
        line = narrate.narrate('model', {'calls': [{'name': 'write_file', 'args': {'path': 'game.py'}},
                                                   {'name': 'dev_start', 'args': {'name': 'game'}}]})
        self.assertEqual(line, 'Decided to: Writing game.py; Launching game')

    def test_failures_checks_and_switches(self):
        self.assertIn('failed', narrate.narrate('tool', {'name': 'read_file', 'output': 'ERROR: Not a file'}))
        self.assertIsNone(narrate.narrate('tool', {'name': 'read_file', 'output': 'ok'}))
        self.assertEqual(narrate.narrate('check', {'name': 'tests', 'status': 'passed', 'phase': 'continuous'}),
                         '  ↳ tests check passed (after edit)')
        self.assertIn('switched to nessa-lfm', narrate.narrate('model_switched', {'previous': 'a/b', 'model': 'nessa-lfm'}))

    def test_unknown_tool_and_missing_args_do_not_crash(self):
        self.assertEqual(narrate._tool('mcp__x__y', {}), 'Using mcp__x__y')
        self.assertEqual(narrate._tool('search', {}), 'Searching for')

    def test_latest_reasoning_and_feed(self):
        activity = [{'event': 'model', 'data': {'reasoning': 'old'}},
                    {'event': 'model', 'data': {'reasoning': 'I should write a tkinter game.', 'calls': []}},
                    {'event': 'check', 'data': {'name': 'syntax', 'status': 'passed'}}]
        self.assertEqual(narrate.latest_reasoning(activity), 'I should write a tkinter game.')
        self.assertEqual(narrate.feed(activity), ['  ↳ syntax check passed'])
        activity.append({'event': 'model', 'data': {'calls': [{'name': 'dev_start', 'args': {}}]}})
        self.assertEqual(narrate.latest_reasoning(activity), 'I should write a tkinter game.')
        self.assertTrue(narrate.latest_reasoning([{'event': 'model', 'data': {'reasoning': 'x ' * 500}}]).endswith('…'))


class GuiHelpers(unittest.TestCase):
    def test_current_turn_starts_at_latest_message(self):
        try:
            from agentharness.gui import activity_text, current_turn
        except ImportError as exc:  # no Tk in this environment
            self.skipTest(str(exc))
        activity = [{'event': 'memory_loaded', 'data': {}}, {'event': 'model', 'data': {'reasoning': 'a'}},
                    {'event': 'memory_loaded', 'data': {}}, {'event': 'model_started', 'data': {'model': 'm'}}]
        self.assertEqual(len(current_turn(activity)), 2)
        self.assertIn('Reasoning: a', activity_text(activity))
