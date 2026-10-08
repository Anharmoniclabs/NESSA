"""Home-directory chat must avoid recursive or whole-home snapshots."""
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from agentharness.__main__ import main


class ChatStart(unittest.TestCase):
    def test_home_chat_uses_small_workspace_and_reaches_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home/'personal.txt').write_text('not part of a coding project')
            output = io.StringIO()
            with patch('pathlib.Path.home', return_value=home), \
                 patch('agentharness.__main__._client', return_value=object()), \
                 patch('builtins.input', return_value='/quit') as prompt, \
                 redirect_stdout(output):
                result = main(['chat', str(home), '--enterprise', '--model', 'test'])
            self.assertEqual(result, 0)
            prompt.assert_called_once_with('you> ')
            self.assertIn('General chat workspace:', output.getvalue())
            sessions = list((home/'.agentharness/runs').iterdir())
            self.assertEqual(len(sessions), 1)
            self.assertEqual((sessions[0]/'DIRECT').read_text(), str(home/'.agentharness/chat'))
            self.assertFalse((sessions[0]/'baseline/personal.txt').exists())

    def test_project_chat_preserves_selected_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home/'project'
            project.mkdir()
            (project/'app.py').write_text('print(42)')
            with patch('pathlib.Path.home', return_value=home), \
                 patch('agentharness.__main__._client', return_value=object()), \
                 patch('builtins.input', return_value='/quit'), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['chat', str(project), '--enterprise', '--model', 'test']), 0)
            session = next((home/'.agentharness/runs').iterdir())
            self.assertEqual((session/'DIRECT').read_text(), str(project))
            self.assertTrue((session/'baseline/app.py').exists())
