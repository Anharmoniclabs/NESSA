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
        self.addCleanup(self.destroy_root)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name) / 'sessions', 'http://127.0.0.1:11435/v1', 'test')
        self.window = Window(self.root, self.app)
        self.root.update()

    def destroy_root(self):
        # Each test owns its Tk interpreter; cancel timers before creating the next one.
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

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

    def test_narrow_window_keeps_composer_and_permission_actions_visible(self):
        self.root.geometry('820x640')
        key = self.app.create()['id']
        self.window.current = key
        self.app.chats[key].data.update(
            busy=True, messages=[dict(role='user', content='Update the project')],
            plan=dict(goal='Allow replace_in_file?', steps=['A long argument preview ' * 8] * 8))
        self.window.tick()
        self.root.update()
        picker, stop, send = self.window.model_picker, self.window.stop_button, self.window.send_button
        self.assertLessEqual(picker.winfo_rootx()+picker.winfo_width(),stop.winfo_rootx())
        self.assertLessEqual(stop.winfo_rootx()+stop.winfo_width(),send.winfo_rootx())
        self.assertLessEqual(send.winfo_rootx()+send.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        for button in (self.window.approve_button,self.window.allow_all_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                 self.window.approval_card.winfo_rooty()+self.window.approval_card.winfo_height())
        self.app.chats[key].data['busy'] = False


if __name__ == '__main__':
    unittest.main()
