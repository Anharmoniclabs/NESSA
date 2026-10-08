import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path
from agentharness.terminal import TerminalUI, safe


class Terminal(unittest.TestCase):
    def ui(self):
        return TerminalUI(Path('/project'),Path('/session'),SimpleNamespace(model='test'),False,plain=True)

    def test_escape_sequences_are_not_executed(self):
        self.assertEqual(safe('\x1b[2Jhello\x1b]52;c;payload\x07\r'), 'hello')

    def test_streamed_answer_is_not_duplicated(self):
        ui=self.ui();out=io.StringIO()
        with redirect_stdout(out):
            ui.begin();ui.delta('Hello ');ui.delta('world')
            ui.answer(SimpleNamespace(summary='Hello world',status='answered'))
        self.assertEqual(out.getvalue().count('Hello world'),1)
        self.assertIn('answered',out.getvalue())

    def test_plain_prompt_and_commands(self):
        ui=self.ui()
        with patch('builtins.input',return_value=' /help ') as reader:
            self.assertEqual(ui.read(),'/help')
        reader.assert_called_once_with('you> ')
        with redirect_stdout(io.StringIO()) as output:
            self.assertTrue(ui.command('/diff',SimpleNamespace(patch=lambda:'No changes.')))
            self.assertTrue(ui.command('/status',None))
        self.assertIn('/project',output.getvalue())
        self.assertFalse(ui.command('hello',None))
        self.assertEqual(ui.complete('/gr',0),'/graph')
