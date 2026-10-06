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


class SelfUpdate(unittest.TestCase):
    def setUp(self):
        try:
            from agentharness import gui
        except ImportError as exc:
            self.skipTest(str(exc))
        self.gui = gui

    def test_restart_only_when_nothing_would_be_lost(self):
        restart = self.gui.should_restart
        self.assertTrue(restart(True, False, '', False))
        self.assertFalse(restart(False, False, '', False))   # no update
        self.assertFalse(restart(True, True, '', False))     # a turn or approval is running
        self.assertFalse(restart(True, False, 'half-typed', False))
        self.assertFalse(restart(True, False, '', True))     # a project is being opened

    def test_code_version_changes_when_a_module_changes(self):
        import os
        import time
        before = self.gui.code_version()
        target = self.gui.CODE / 'narrate.py'
        stat = target.stat()
        try:
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertNotEqual(self.gui.code_version(), before)
        finally:
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.gui.code_version(), before)
