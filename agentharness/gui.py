"""NESSA native desktop chat: python -m agentharness.gui [PROJECT]."""
from __future__ import annotations
import argparse
import fcntl
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from . import panels
from .desktop import App

BG = '#202022'
PANEL = '#19191b'
SURFACE = '#2b2b2f'
TEXT = '#eeece8'
MUTED = '#9b999f'
ACCENT = '#d4c4f5'
BORDER = '#3c3b40'
FONT = 'DejaVu Sans'
PANELS = ('Timeline', 'Checks', 'Context', 'Reviewer', 'Processes')  # details tabs after Activity and Changes


class RoundedSurface(tk.Canvas):
    """Resizable native-Tk surface with a rounded border and child content."""
    def __init__(self, parent, color=SURFACE, radius=22, **kwargs):
        super().__init__(parent, bg=BG, highlightthickness=0, **kwargs)
        self.color, self.radius = color, radius
        self.body = tk.Frame(self, bg=color)
        self.window = self.create_window(14, 12, anchor='nw', window=self.body)
        self.bind('<Configure>', self.resize)

    def resize(self, event):
        w, h, r = event.width-1, event.height-1, self.radius
        points = [r, 1, w-r, 1, w, 1, w, r, w, h-r, w, h, w-r, h,
                  r, h, 1, h, 1, h-r, 1, r, 1, 1]
        self.delete('surface')
        self.create_polygon(points, smooth=True, splinesteps=24, fill=self.color,
                            outline=BORDER, tags='surface')
        self.tag_lower('surface')
        self.itemconfigure(self.window, width=max(1, w-28), height=max(1, h-24))


class Window:
    def __init__(self, root, app, project=None):
        self.root, self.app = root, app
        self.current = None
        self.ids = []
        self.rendered = None
        self.started = time.monotonic()
        self.creating = False
        self.creation = queue.Queue()
        self.drafts = {}
        self._apply_theme()
        self._build_sidebar()
        self._build_conversation()
        self._build_hero()
        self._build_approval()
        self._build_composer()
        self._build_details()
        root.bind('<Control-n>', lambda _: self.new())
        self.refresh_history()
        self.new()
        if project:
            self.root.after(100, lambda: self.create(project))
        self.tick()

    def _apply_theme(self):
        root = self.root
        root.title('Nessa')
        root.geometry('1200x840')
        root.minsize(780, 620)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=TEXT, font=(FONT, 10))
        style.configure('TButton', background=SURFACE, foreground=TEXT, padding=(13, 9), borderwidth=0, font=(FONT, 10))
        style.map('TButton', background=[('active', '#38383d'), ('disabled', SURFACE)], foreground=[('disabled', '#737178')])
        style.configure('Ghost.TButton', background=BG, foreground=MUTED)
        style.map('Ghost.TButton', background=[('active', SURFACE)], foreground=[('active', TEXT)])
        style.configure('Side.TButton', background=PANEL, anchor='w', padding=(12, 11))
        style.map('Side.TButton', background=[('active', SURFACE)])
        style.configure('Accent.TButton', background=ACCENT, foreground='#26222d', padding=(14, 8))
        style.map('Accent.TButton', background=[('active', '#e3d8fa'), ('disabled', '#49434f')], foreground=[('disabled', MUTED)])
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background=PANEL, foreground=TEXT, padding=(15, 8))
        style.map('TNotebook.Tab', background=[('selected', SURFACE)])
        style.configure('Vertical.TScrollbar', background=SURFACE, troughcolor=BG, borderwidth=0, arrowsize=10)

    def _build_sidebar(self):
        root = self.root
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(root, bg=PANEL, width=238, padx=16, pady=22)
        self.sidebar.grid(row=0, column=0, sticky='nsew')
        self.sidebar.grid_propagate(False)
        brand = tk.Frame(self.sidebar, bg=PANEL)
        brand.pack(fill='x', pady=(0, 26))
        tk.Label(brand, text='✳', bg=PANEL, fg=ACCENT, font=(FONT, 24)).pack(side='left')
        tk.Label(brand, text='nessa', bg=PANEL, fg=TEXT, font=(FONT, 20, 'bold')).pack(side='left', padx=8)
        self.new_button = ttk.Button(self.sidebar, text='+   New chat', style='Side.TButton', command=self.new)
        self.new_button.pack(fill='x')
        self.project_button = ttk.Button(self.sidebar, text='↗   Project chat', style='Side.TButton', command=self.project_chat)
        self.project_button.pack(fill='x', pady=(3, 22))
        self.search = tk.StringVar()
        tk.Label(self.sidebar, text='Search conversations', bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(anchor='w', padx=10, pady=(0, 8))
        search = tk.Entry(self.sidebar, textvariable=self.search, bg='#242426', fg=TEXT, insertbackground=TEXT,
                          relief='flat', font=(FONT, 10), highlightthickness=1, highlightbackground='#303034')
        search.pack(fill='x', ipady=7)
        self.search.trace_add('write', lambda *_: self.refresh_history())
        tk.Label(self.sidebar, text='RECENT', bg=PANEL, fg='#77757c', font=(FONT, 8, 'bold')).pack(anchor='w', padx=10, pady=(26, 12))
        self.history = tk.Listbox(self.sidebar, bg=PANEL, fg='#c6c3cb', selectbackground='#323035', selectforeground=TEXT,
                                  borderwidth=0, highlightthickness=0, activestyle='none', font=(FONT, 10), exportselection=False)
        self.history.pack(fill='both', expand=True)
        self.history.bind('<<ListboxSelect>>', self.select)
        tk.Frame(self.sidebar, bg='#2b292e', height=1).pack(fill='x', pady=18)
        tk.Label(self.sidebar, text='●  On-device assistant', bg=PANEL, fg='#a8bea9', font=(FONT, 9), anchor='w').pack(fill='x', padx=8)
        tk.Label(self.sidebar, text='Your space. Your pace.', bg=PANEL, fg='#79767f', font=(FONT, 9), anchor='w').pack(fill='x', padx=8, pady=(6, 0))

    def _build_conversation(self):
        root = self.root
        main = tk.Frame(root, bg=BG)
        main.grid(row=0, column=1, sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        top = tk.Frame(main, bg=BG, padx=22, pady=16)
        top.grid(row=0, column=0, sticky='ew')
        ttk.Button(top, text='☰', style='Ghost.TButton', command=self.toggle_sidebar, width=3).pack(side='left')
        tk.Label(top, text='Nessa', bg=BG, fg=TEXT, font=(FONT, 13, 'bold')).pack(side='left', padx=(10, 8))
        tk.Label(top, text='LOCAL', bg=SURFACE, fg=MUTED, font=(FONT, 8), padx=8, pady=4).pack(side='left')
        ttk.Button(top, text='Changes', style='Ghost.TButton', command=lambda: self.show_details(1)).pack(side='right')
        ttk.Button(top, text='Activity', style='Ghost.TButton', command=lambda: self.show_details(0)).pack(side='right')
        viewport = tk.Frame(main, bg=BG)
        viewport.grid(row=1, column=0, sticky='nsew')
        self.content = tk.Frame(viewport, bg=BG)
        self.content.place(relx=.5, rely=0, anchor='n', relheight=1)
        viewport.bind('<Configure>', lambda e: self.content.place_configure(width=min(820, max(1, e.width-56))))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)
        self.project = ttk.Label(self.content, text='', foreground=MUTED, wraplength=660, font=(FONT, 9))
        self.project.grid(row=0, column=0, sticky='w', pady=(0, 8))
        self.conversation = tk.Frame(self.content, bg=BG)
        self.conversation.grid(row=1, column=0, sticky='nsew')
        self.chat = self.text_panel(self.conversation)
        self.chat.configure(font=(FONT, 11), spacing1=4, spacing3=10, padx=12, pady=22)
        self.chat.tag_configure('user', foreground=MUTED, font=(FONT, 9, 'bold'), spacing1=18, spacing3=8)
        self.chat.tag_configure('assistant', foreground=ACCENT, font=(FONT, 10, 'bold'), spacing1=22, spacing3=8)
        self.chat.tag_configure('user_body', background=SURFACE, lmargin1=16, lmargin2=16, rmargin=16, spacing1=10, spacing3=14)
        self.chat.tag_configure('meta', foreground=MUTED, font=(FONT, 9))
        self.chat.tag_configure('bold', font=(FONT, 11, 'bold'))
        self.chat.tag_configure('heading', font=(FONT, 14, 'bold'), spacing1=14, spacing3=10)
        self.chat.tag_configure('code', background=PANEL, foreground='#d3cbe1', font=('DejaVu Sans Mono', 10), lmargin1=16, lmargin2=16, spacing1=4, spacing3=4)

    def _build_hero(self):
        root = self.root
        self.hero = tk.Frame(self.content, bg=BG)
        self.hero.grid(row=1, column=0, sticky='nsew')
        intro = tk.Frame(self.hero, bg=BG)
        intro.place(relx=.5, rely=.47, anchor='center', relwidth=1)
        tk.Label(intro, text='✳', bg=BG, fg=ACCENT, font=(FONT, 42)).pack(pady=(0, 16))
        tk.Label(intro, text='Where shall we start?', bg=BG, fg=TEXT, font=('DejaVu Serif', 27)).pack()
        tk.Label(intro, text='A question, an idea, a fresh perspective.', bg=BG, fg=MUTED, font=(FONT, 11)).pack(pady=(14, 28))
        suggestions = tk.Frame(intro, bg=BG)
        suggestions.pack()
        for title, prompt in [('Explore an idea', 'Help me think through an idea: '), ('Make a plan', 'Help me make a plan for '), ('Explain something', 'Explain this to me: ')]:
            ttk.Button(suggestions, text=title, command=lambda p=prompt: self.suggest(p)).pack(side='left', padx=4)

    def _build_approval(self):
        root = self.root
        self.approval = ttk.Frame(self.content, padding=(0, 10))
        self.approval.columnconfigure(0, weight=1)
        self.plan_label = ttk.Label(self.approval, wraplength=620)
        self.plan_label.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
        ttk.Button(self.approval, text='Approve plan', style='Accent.TButton', command=lambda: self.decide(True)).grid(row=1, column=0, sticky='w')
        ttk.Button(self.approval, text='Reject', command=lambda: self.decide(False)).grid(row=1, column=1, sticky='e')
        self.status = ttk.Label(self.content, text='', foreground=MUTED, font=(FONT, 9))
        self.status.grid(row=4, column=0, sticky='w', padx=10, pady=(6, 8))

    def _build_composer(self):
        root = self.root
        compose = RoundedSurface(self.content, height=144)
        compose.grid(row=5, column=0, sticky='ew')
        compose.body.columnconfigure(0, weight=1)
        compose.body.rowconfigure(0, weight=1)
        self.input = tk.Text(compose.body, height=2, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, wrap='word',
                             font=(FONT, 11), relief='flat', padx=6, pady=6, highlightthickness=0, undo=True)
        self.input.grid(row=0, column=0, sticky='nsew')
        self.placeholder = tk.Label(self.input, text='Message Nessa…', bg=SURFACE, fg=MUTED, font=(FONT, 11))
        self.placeholder.place(x=6, y=6)
        self.placeholder.bind('<Button-1>', lambda _: self.input.focus_set())
        self.input.bind('<KeyRelease>', self.composer_changed)
        self.input.bind('<<Modified>>', self.composer_changed)
        self.input.bind('<Return>', self.enter)
        controls = tk.Frame(compose.body, bg=SURFACE)
        controls.grid(row=1, column=0, sticky='ew', pady=(6, 0))
        self.model_label = tk.Label(controls, text='✳  Fast chat', bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.model_label.pack(side='left', padx=8)
        self.send_button = ttk.Button(controls, text='↑', width=3, style='Accent.TButton', command=self.send)
        self.send_button.pack(side='right')
        self.stop_button = ttk.Button(controls, text='Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=8)
        self.stop_button.pack_forget()
        ttk.Label(self.content, text='Local by design   ·   Enter to send, Shift+Enter for a new line', foreground='#7f7c85', font=(FONT, 8), anchor='center').grid(row=6, column=0, sticky='ew', pady=(12, 18))

    def _build_details(self):
        root = self.root
        self.details = tk.Toplevel(root)
        self.details.withdraw()
        self.details.title('Nessa · Workspace')
        self.details.geometry('800x580')
        self.details.configure(bg=BG)
        self.details.protocol('WM_DELETE_WINDOW', self.details.withdraw)
        actions = ttk.Frame(self.details, padding=(18, 12, 18, 0))
        actions.pack(fill='x')
        self.apply_button = ttk.Button(actions, text='Apply to project', style='Accent.TButton',
                                       command=self.apply_changes, state='disabled')
        self.apply_button.pack(side='right')
        ttk.Label(actions, text='Changes stay in a private copy until you apply them.',
                  foreground=MUTED, font=(FONT, 9)).pack(side='left')
        self.tabs = ttk.Notebook(self.details)
        self.tabs.pack(fill='both', expand=True, padx=18, pady=18)
        self.activity = self.text_tab(self.tabs, 'Activity')
        self.patch = self.text_tab(self.tabs, 'Changes')
        self.panes = {title: self.text_tab(self.tabs, title) for title in PANELS}

    def text_panel(self, frame):
        text = tk.Text(frame, bg=BG, fg=TEXT, insertbackground=TEXT, wrap='word', font=('DejaVu Sans Mono', 10),
                       relief='flat', padx=16, pady=18, state='disabled', highlightthickness=0)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def composer_changed(self, _=None):
        if self.input.get('1.0', 'end-1c'):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=6, y=6)
        if self.input.edit_modified():
            self.input.edit_modified(False)

    def suggest(self, prompt):
        self.input.delete('1.0', 'end')
        self.input.insert('1.0', prompt)
        self.input.focus_set()
        self.input.mark_set('insert', 'end-1c')
        self.composer_changed()

    def toggle_sidebar(self):
        if self.sidebar.winfo_ismapped():
            self.sidebar.grid_remove()
        else:
            self.sidebar.grid()

    def show_details(self, index):
        self.tabs.select(index)
        self.details.deiconify()
        self.details.lift()

    def render_reply(self, content):
        import re
        code = False
        for line in content.splitlines(keepends=True):
            if line.strip().startswith('```'):
                code = not code
                self.chat.insert('end', '\n', 'code')
            elif code:
                self.chat.insert('end', line, 'code')
            elif re.match(r'^#{1,6}\s', line):
                self.chat.insert('end', re.sub(r'^#{1,6}\s+', '', line), 'heading')
            else:
                for part in re.split(r'(\*\*[^*]+\*\*|`[^`]+`)', line):
                    if part.startswith('**') and part.endswith('**'):
                        self.chat.insert('end', part[2:-2], 'bold')
                    elif part.startswith('`') and part.endswith('`'):
                        self.chat.insert('end', part[1:-1], 'code')
                    else:
                        self.chat.insert('end', part)
        self.chat.insert('end', '\n\n')

    def text_tab(self, tabs, title):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text=title)
        return self.text_panel(frame)

    def set_text(self, widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('end', text)
        widget.configure(state='disabled')

    def refresh_history(self):
        with self.app.lock:
            self.ids = [key for key in reversed(self.app.chats) if self.search.get().casefold() in self.app.snapshot(key)['title'].casefold()]
        self.history.delete(0, 'end')
        for key in self.ids:
            self.history.insert('end', self.app.snapshot(key)['title'])
        if self.current in self.ids:
            self.history.selection_set(self.ids.index(self.current))

    def select(self, _):
        selection = self.history.curselection()
        if selection:
            self.drafts[self.current] = self.input.get('1.0', 'end-1c')
            self.current = self.ids[selection[0]]
            self.input.delete('1.0', 'end')
            self.input.insert('1.0', self.drafts.get(self.current, ''))
            self.rendered = None

    def new(self):
        if self.creating:
            return
        self.drafts[self.current] = self.input.get('1.0', 'end-1c')
        self.current = None
        self.rendered = None
        self.history.selection_clear(0, 'end')
        self.set_text(self.chat, '')
        self.conversation.grid_remove()
        self.hero.grid()
        self.set_text(self.activity, '')
        self.set_text(self.patch, 'No file changes yet.')
        self.project.configure(text='')
        self.status.configure(text='')
        self.approval.grid_remove()
        self.send_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self.input.delete('1.0', 'end')
        self.input.insert('1.0', self.drafts.get(None, ''))
        self.input.focus_set()
        self.stop_button.pack_forget()

    def project_chat(self):
        project = filedialog.askdirectory(title='Choose a project for Nessa')
        if project:
            self.create(project)

    def create(self, project):
        if self.creating:
            return
        self.creating = True
        self.new_button.configure(state='disabled')
        self.project_button.configure(state='disabled')
        self.send_button.configure(state='disabled')
        self.status.configure(text='Preparing a private project copy…')
        def work():
            try:
                self.creation.put((self.app.create(project)['id'], None))
            except Exception as exc:
                self.creation.put((None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def enter(self, event):
        if event.state & 1:
            return None
        self.send()
        return 'break'

    def send(self):
        message = self.input.get('1.0', 'end').strip()
        if not message or self.creating:
            return
        try:
            if not self.current:
                self.current = self.app.create()['id']
            self.app.send(self.current, message)
            self.input.delete('1.0', 'end')
            self.drafts.pop(self.current, None)
            self.drafts.pop(None, None)
            self.started = time.monotonic()
            self.refresh_history()
        except Exception as exc:
            messagebox.showerror('Unable to send', str(exc))

    def decide(self, approved):
        if self.current:
            self.app.decide(self.current, approved)
            self.approval.grid_remove()

    def apply_changes(self):
        if not self.current:
            return
        d = self.app.snapshot(self.current)
        files = '\n'.join(d.get('result', {}).get('changed_files') or [])
        if not messagebox.askyesno('Apply changes', f"Write these files into {d['project']}?\n\n{files}"):
            return
        try:
            self.app.apply(self.current)
        except ValueError as exc:
            messagebox.showerror('Not applied', str(exc))

    def stop(self):
        if self.current:
            self.app.stop(self.current)

    def _poll_creation(self):
        """Pick up a finished project-copy request from the background thread."""
        try:
            key, error = self.creation.get_nowait()
        except queue.Empty:
            return
        self.creating = False
        self.new_button.configure(state='normal')
        self.project_button.configure(state='normal')
        self.send_button.configure(state='normal')
        if error:
            messagebox.showerror('Unable to open project', error)
            self.status.configure(text='Could not create a project copy.')
        else:
            self.current = key
            self.rendered = None
            self.refresh_history()

    def _render_conversation(self, d):
        """Redraw the transcript only when its messages or the streaming partial changed."""
        fingerprint = json.dumps([self.current, d['messages'], d.get('partial')])
        if fingerprint == self.rendered:
            return
        if d['messages']:
            self.hero.grid_remove()
            self.conversation.grid()
        else:
            self.conversation.grid_remove()
            self.hero.grid()
        self.chat.configure(state='normal')
        self.chat.delete('1.0', 'end')
        for m in d['messages']:
            self.chat.insert('end', ('You' if m['role'] == 'user' else '✳  Nessa') + '\n', m['role'])
            if m['role'] == 'user':
                self.chat.insert('end', m['content'] + '\n\n', 'user_body')
            else:
                self.render_reply(m['content'])
            if m.get('status') not in (None, 'answered'):
                self.chat.insert('end', m['status'].replace('_', ' ') + '\n\n', 'meta')
        if d.get('partial'):
            self.chat.insert('end', '✳  Nessa\n', 'assistant')
            self.render_reply(d['partial'])
        self.chat.configure(state='disabled')
        self.chat.see('end')
        self.rendered = fingerprint

    def _render_details(self, d):
        """Refresh the Activity and Changes tabs and the Apply button."""
        activity = '\n\n'.join(e['event'].replace('_', ' ').upper() + '\n' +
                                json.dumps(e['data'], ensure_ascii=False, indent=2) for e in d['activity'])
        if self.activity.get('1.0', 'end-1c') != activity:
            self.set_text(self.activity, activity)
            self.activity.see('end')
        result = d.get('result') or {}
        patch = result.get('patch') or 'No file changes yet.'
        if result.get('evidence_dir'):
            patch = 'Evidence: ' + result['evidence_dir'] + '\n\n' + patch
        if self.patch.get('1.0', 'end-1c') != patch:
            self.set_text(self.patch, patch)
        activity_events = d['activity']
        texts = {'Timeline': panels.timeline(activity_events),
                 'Checks': panels.checks(activity_events, result, result.get('evidence_dir', '')),
                 'Context': panels.context(activity_events),
                 'Reviewer': panels.reviewer(activity_events),
                 'Processes': panels.processes(activity_events)}
        for title, text in texts.items():
            if self.panes[title].get('1.0', 'end-1c') != text:
                self.set_text(self.panes[title], text)
        applyable = (d['project'] and not d['busy'] and result.get('patch')
                     and result.get('status') in ('verified', 'unverified'))
        self.apply_button.configure(state='normal' if applyable else 'disabled')

    def _status_text(self, d) -> str:
        """What the status line says right now. Stopping and streaming outrank a pending plan."""
        if d['busy'] and self.app.chats[self.current].cancel.is_set():
            return 'Stopping at the next response or tool boundary…'
        if d['busy'] and d.get('partial'):
            return 'Nessa is replying…'
        if d['plan']:
            return 'Waiting for your plan approval'
        if not d['busy']:
            return ''
        status = ('Nessa is thinking' + '.' * (1 + int(time.monotonic()) % 3) + '   ' +
                  str(int(time.monotonic() - self.started)) + 's')
        last_tool = next((e for e in reversed(d['activity'])
                          if e['event'] in ('operation_started', 'operation_finished', 'model_started')), None)
        if last_tool and last_tool['event'] == 'operation_started':
            status = 'Using ' + last_tool['data'].get('name', 'tool') + '…'
        return status

    def _sync_controls(self, d):
        busy = d['busy']
        self.send_button.configure(state='disabled' if busy or self.creating else 'normal')
        if busy:
            self.stop_button.pack(side='right', padx=8)
        else:
            self.stop_button.pack_forget()
        self.stop_button.configure(state='normal' if busy else 'disabled')
        plan = d['plan']
        if plan:
            self.plan_label.configure(text='PLAN APPROVAL\n' + plan.get('goal', '') + '\n' +
                                      '\n'.join(str(step) for step in plan.get('steps', [])))
            self.approval.grid(row=3, column=0, sticky='ew')
        else:
            self.approval.grid_remove()
        active = d.get('active_model', '')
        self.model_label.configure(text='✳  ' + ('Local LFM' if active.startswith('nessa-lfm:') else 'Local chat'))
        if not self.creating:
            self.status.configure(text=self._status_text(d))

    def tick(self):
        self._poll_creation()
        if self.current:
            d = self.app.snapshot(self.current)
            self.project.configure(text=('Project · ' + Path(d['project']).name) if d['project'] else '')
            self._render_conversation(d)
            self._render_details(d)
            self._sync_controls(d)
        self.root.after(400, self.tick)

    def close(self):
        if self.creating or any(c.data['busy'] for c in self.app.chats.values()):
            messagebox.showinfo('Nessa is working', 'Use Stop and wait for the current operation to return before closing. Project copying also needs to finish.')
            return
        self.app.shutdown()
        self.root.destroy()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('project', nargs='?')
    p.add_argument('--base-url', default='http://127.0.0.1:11435/v1')
    p.add_argument('--model', default='nessa-lfm:latest')
    p.add_argument('--chat-model', default='nessa-lfm:latest')
    p.add_argument('--chat-base-url', default='http://127.0.0.1:11435/v1')
    p.add_argument('--review-model', help='optional advisory reviewer; disabled by default')
    p.add_argument('--review-base-url', help='reviewer endpoint; defaults to the project model endpoint')
    p.add_argument('--sessions', type=Path, default=Path.home() / '.agentharness/desktop-chats')
    args = p.parse_args()
    root = tk.Tk()
    args.sessions.mkdir(parents=True, exist_ok=True)
    lock = (args.sessions / '.gui.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        messagebox.showinfo('Nessa is already open', 'A Nessa window is already using these conversations.')
        root.destroy()
        return
    Window(root, App(args.sessions, args.base_url, args.model, args.chat_model, args.chat_base_url,
                     args.review_model, args.review_base_url), args.project)
    root.mainloop()


if __name__ == '__main__':
    main()
