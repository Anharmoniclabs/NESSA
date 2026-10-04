"""Native UI flow checks; skipped when a display is unavailable."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentharness.desktop import App


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            from agentharness.gui import Window
            self.root = tk.Tk()
        except (ImportError, RuntimeError) as exc:
            self.skipTest(str(exc))
        except Exception as exc:
            if type(exc).__name__ == 'TclError':
                self.skipTest(str(exc))
            raise
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name) / 'sessions', 'http://127.0.0.1:11435/v1', 'test')
        self.window = Window(self.root, self.app)
        self.root.update()

    def test_launch_and_new_chat_send_without_picker(self):
        with patch('agentharness.gui.filedialog.askdirectory') as picker, patch.object(self.app, 'send') as send:
            self.assertIsNone(self.window.current)
            self.assertEqual(str(self.window.send_button['state']), 'normal')
            self.window.input.insert('1.0', 'Hello Nessa')
            self.window.send()
            first = self.window.current
            send.assert_called_once_with(first, 'Hello Nessa')
            self.assertIsNone(self.app.snapshot(first)['project'])
            self.window.new()
            self.window.input.insert('1.0', 'Another question')
            self.window.send()
            self.assertNotEqual(first, self.window.current)
            picker.assert_not_called()

    def test_project_chat_remains_optional(self):
        with patch('agentharness.gui.filedialog.askdirectory', return_value='/project') as picker, patch.object(self.window, 'create') as create:
            self.window.project_chat()
            picker.assert_called_once()
            create.assert_called_once_with('/project')

    def test_history_search_and_drafts(self):
        first = self.app.create()['id']
        second = self.app.create()['id']
        self.app.chats[first].data['title'] = 'Morning ideas'
        self.app.chats[second].data['title'] = 'Python question'
        self.window.refresh_history()
        self.window.search.set('python')
        self.assertEqual(self.window.ids, [second])
        self.window.search.set('')
        self.window.current = first
        self.window.input.insert('1.0', 'An unfinished thought')
        self.window.history.selection_set(self.window.ids.index(second))
        self.window.select(None)
        self.assertEqual(self.window.input.get('1.0', 'end-1c'), '')
        self.window.history.selection_clear(0, 'end')
        self.window.history.selection_set(self.window.ids.index(first))
        self.window.select(None)
        self.assertEqual(self.window.input.get('1.0', 'end-1c'), 'An unfinished thought')

    def test_suggestions_and_details_do_not_send(self):
        with patch.object(self.app, 'send') as send:
            self.window.suggest('Explain this to me: ')
            self.assertEqual(self.window.input.get('1.0', 'end-1c'), 'Explain this to me: ')
            self.window.show_details(1)
            self.assertEqual(self.window.tabs.index('current'), 1)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
