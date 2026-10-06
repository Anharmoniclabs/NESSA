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

from . import narrate
from .desktop import App

BRAND = Path(__file__).resolve().parents[1] / 'brand'
# NESSA identity: ink surfaces, a violet→cyan "resonance" accent, semantic status colours.
BG = '#0F1115'        # ink
PANEL = '#0B0D11'     # deep ink (sidebar)
SURFACE = '#171B22'   # raised surface
RAISED = '#1F242D'    # hover / chips
TEXT = '#E8EAED'
MUTED = '#8A93A0'
FAINT = '#5D6571'
ACCENT = '#8B7CFF'    # resonance violet
SIGNAL = '#4FD8C4'    # signal cyan
OK = '#5BD6A0'
WARN = '#F5B454'
ERROR = '#F2777A'
BORDER = '#262C36'
FONT = 'Inter'
DISPLAY = 'Inter Display'
MONO = 'JetBrains Mono'


def brand_image(name):
    """Brand PNG as a Tk image, or None when the asset is missing (the UI falls back to text)."""
    path = BRAND / name
    try:
        return tk.PhotoImage(file=str(path)) if path.exists() else None
    except tk.TclError:
        return None


class RoundedSurface(tk.Canvas):
    """Resizable native-Tk surface with a rounded border and child content."""
    def __init__(self, parent, color=SURFACE, radius=20, outline=BORDER, **kwargs):
        super().__init__(parent, bg=parent['bg'] if 'bg' in parent.keys() else BG, highlightthickness=0, **kwargs)
        self.color, self.radius, self.outline = color, radius, outline
        self.body = tk.Frame(self, bg=color)
        self.window = self.create_window(16, 12, anchor='nw', window=self.body)
        self.bind('<Configure>', self.resize)

    def set_outline(self, color):
        self.outline = color
        self.itemconfigure('surface', outline=color)

    def resize(self, event):
        w, h, r = event.width-1, event.height-1, self.radius
        points = [r, 1, w-r, 1, w, 1, w, r, w, h-r, w, h, w-r, h,
                  r, h, 1, h, 1, h-r, 1, r, 1, 1]
        self.delete('surface')
        self.create_polygon(points, smooth=True, splinesteps=24, fill=self.color,
                            outline=self.outline, tags='surface')
        self.tag_lower('surface')
        self.itemconfigure(self.window, width=max(1, w-32), height=max(1, h-24))


def chip(parent, text, fg=MUTED, bg=RAISED):
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 8, 'bold'), padx=9, pady=4)


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
        self.cloud = bool(getattr(app, 'cloud', False))
        self.enterprise = bool(getattr(app, 'enterprise', False))
        root.title('Nessa')
        root.geometry('1240x860')
        root.minsize(820, 640)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.icons = [img for img in (brand_image(f'icon-{s}.png') for s in (256, 128, 64, 48, 32)) if img]
        if self.icons:
            root.iconphoto(True, *self.icons)
        self.glyph_small = brand_image('glyph-22.png')
        self.glyph_side = brand_image('glyph-28.png')
        self.glyph_hero = brand_image('glyph-96.png')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=TEXT, font=(FONT, 10))
        style.configure('TButton', background=SURFACE, foreground=TEXT, padding=(14, 9), borderwidth=0,
                        font=(FONT, 10), focuscolor=SURFACE)
        style.map('TButton', background=[('active', RAISED), ('disabled', SURFACE)], foreground=[('disabled', FAINT)])
        style.configure('Ghost.TButton', background=BG, foreground=MUTED, padding=(12, 7))
        style.map('Ghost.TButton', background=[('active', SURFACE)], foreground=[('active', TEXT)])
        style.configure('Side.TButton', background=PANEL, foreground='#C9CED6', anchor='w', padding=(12, 10))
        style.map('Side.TButton', background=[('active', SURFACE)], foreground=[('active', TEXT)])
        style.configure('Primary.TButton', background=ACCENT, foreground='#0F1115', anchor='w', padding=(12, 10),
                        font=(FONT, 10, 'bold'))
        style.map('Primary.TButton', background=[('active', '#A398FF'), ('disabled', '#3A3560')])
        style.configure('Accent.TButton', background=ACCENT, foreground='#0F1115', padding=(14, 8), font=(FONT, 10, 'bold'),
                        focuscolor=ACCENT)
        style.map('Accent.TButton', background=[('active', '#A398FF'), ('disabled', '#2E2A4D')], foreground=[('disabled', FAINT)])
        style.configure('Warn.TButton', background=WARN, foreground='#1A1408', padding=(14, 8), font=(FONT, 10, 'bold'),
                        focuscolor=WARN)
        style.map('Warn.TButton', background=[('active', '#F8C676')])
        style.configure('Suggest.TButton', background=SURFACE, foreground='#C9CED6', padding=(16, 12), font=(FONT, 10))
        style.map('Suggest.TButton', background=[('active', RAISED)], foreground=[('active', TEXT)])
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background=PANEL, foreground=MUTED, padding=(16, 8), font=(FONT, 10))
        style.map('TNotebook.Tab', background=[('selected', SURFACE)], foreground=[('selected', TEXT)])
        # Slim, arrowless scrollbar in brand colours.
        style.layout('Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
            ('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        style.configure('Vertical.TScrollbar', background=RAISED, troughcolor=BG, borderwidth=0, relief='flat',
                        bordercolor=BG, lightcolor=RAISED, darkcolor=RAISED, gripcount=0, width=8)
        style.map('Vertical.TScrollbar', background=[('active', '#2D3440')])
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ---- sidebar: brand, actions, history, connection status
        self.sidebar = tk.Frame(root, bg=PANEL, width=256, padx=16, pady=20)
        self.sidebar.grid(row=0, column=0, sticky='nsew')
        self.sidebar.grid_propagate(False)
        tk.Frame(root, bg=BORDER, width=1).grid(row=0, column=0, sticky='nse')
        brand = tk.Frame(self.sidebar, bg=PANEL)
        brand.pack(fill='x', pady=(2, 24), padx=6)
        if self.glyph_side:
            tk.Label(brand, image=self.glyph_side, bg=PANEL).pack(side='left')
        else:
            tk.Label(brand, text='N', bg=PANEL, fg=ACCENT, font=(DISPLAY, 18, 'bold')).pack(side='left')
        tk.Label(brand, text='nessa', bg=PANEL, fg=TEXT, font=(DISPLAY, 19, 'bold')).pack(side='left', padx=(10, 0))
        self.new_button = ttk.Button(self.sidebar, text='+   New chat', style='Primary.TButton', command=self.new)
        self.new_button.pack(fill='x')
        self.project_button = ttk.Button(self.sidebar, text='▸   Open a project', style='Side.TButton',
                                         command=self.project_chat)
        self.project_button.pack(fill='x', pady=(6, 20))
        self.search = tk.StringVar()
        search_box = tk.Frame(self.sidebar, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        search_box.pack(fill='x')
        tk.Label(search_box, text='⌕', bg=SURFACE, fg=FAINT, font=(FONT, 11)).pack(side='left', padx=(10, 2))
        search = tk.Entry(search_box, textvariable=self.search, bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
                          relief='flat', font=(FONT, 10), highlightthickness=0)
        search.pack(side='left', fill='x', expand=True, ipady=8, padx=(2, 8))
        self.search.trace_add('write', lambda *_: self.refresh_history())
        tk.Label(self.sidebar, text='RECENT', bg=PANEL, fg=FAINT, font=(FONT, 8, 'bold')).pack(anchor='w', padx=8, pady=(22, 8))
        self.history = tk.Listbox(self.sidebar, bg=PANEL, fg='#B8BEC8', selectbackground=RAISED, selectforeground=TEXT,
                                  borderwidth=0, highlightthickness=0, activestyle='none', font=(FONT, 10),
                                  exportselection=False)
        self.history.pack(fill='both', expand=True, padx=2)
        self.history.bind('<<ListboxSelect>>', self.select)
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill='x', pady=(14, 14))
        footer = tk.Frame(self.sidebar, bg=PANEL)
        footer.pack(fill='x', padx=6)
        self.connection_dot = tk.Label(footer, text='●', bg=PANEL, fg=OK, font=(FONT, 9))
        self.connection_dot.pack(side='left')
        self.connection = tk.Label(footer, text='Ready', bg=PANEL, fg='#B8BEC8', font=(FONT, 9), anchor='w')
        self.connection.pack(side='left', padx=(6, 0))
        tk.Label(self.sidebar, text='Anharmonic Labs', bg=PANEL, fg=FAINT, font=(FONT, 8), anchor='w').pack(
            fill='x', padx=6, pady=(8, 0))

        # ---- main column: top bar, conversation, composer
        main = tk.Frame(root, bg=BG)
        main.grid(row=0, column=1, sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        top = tk.Frame(main, bg=BG, padx=20, pady=14)
        top.grid(row=0, column=0, sticky='ew')
        ttk.Button(top, text='☰', style='Ghost.TButton', command=self.toggle_sidebar, width=3).pack(side='left')
        self.title_label = tk.Label(top, text='New conversation', bg=BG, fg=TEXT, font=(FONT, 12, 'bold'))
        self.title_label.pack(side='left', padx=(10, 12))
        chip(top, 'CLOUD + LOCAL' if self.cloud else 'ON-DEVICE', fg=SIGNAL if self.cloud else OK).pack(side='left')
        if self.enterprise:
            chip(top, 'EDITS IN PLACE', fg=WARN).pack(side='left', padx=(6, 0))
        ttk.Button(top, text='Changes', style='Ghost.TButton', command=lambda: self.show_details(1)).pack(side='right')
        ttk.Button(top, text='Activity', style='Ghost.TButton', command=lambda: self.show_details(0)).pack(side='right')
        tk.Frame(main, bg=BORDER, height=1).grid(row=0, column=0, sticky='sew')
        viewport = tk.Frame(main, bg=BG)
        viewport.grid(row=1, column=0, sticky='nsew')
        self.content = tk.Frame(viewport, bg=BG)
        self.content.place(relx=.5, rely=0, anchor='n', relheight=1)
        viewport.bind('<Configure>', lambda e: self.content.place_configure(width=min(860, max(1, e.width-56))))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)
        self.project = tk.Label(self.content, text='', bg=BG, fg=MUTED, font=(FONT, 9), anchor='w')
        self.project.grid(row=0, column=0, sticky='ew', pady=(14, 0))
        self.conversation = tk.Frame(self.content, bg=BG)
        self.conversation.grid(row=1, column=0, sticky='nsew')
        self.chat = self.text_panel(self.conversation)
        self.chat.configure(font=(FONT, 11), spacing1=3, spacing2=3, spacing3=8, padx=8, pady=20)
        self.chat.tag_configure('user', foreground=MUTED, font=(FONT, 9, 'bold'), spacing1=20, spacing3=8)
        self.chat.tag_configure('assistant', foreground=TEXT, font=(FONT, 10, 'bold'), spacing1=22, spacing3=8)
        self.chat.tag_configure('user_body', background=SURFACE, lmargin1=18, lmargin2=18, rmargin=18,
                                spacing1=12, spacing3=14)
        self.chat.tag_configure('meta', foreground=MUTED, font=(FONT, 9), lmargin1=18, lmargin2=30)
        self.chat.tag_configure('status_meta', foreground=FAINT, font=(FONT, 8, 'bold'), spacing3=6)
        self.chat.tag_configure('working', foreground=SIGNAL, font=(FONT, 9, 'bold'), spacing1=22, spacing3=6)
        self.chat.tag_configure('thinking', foreground='#AEB4FF', font=(FONT, 10, 'italic'), lmargin1=18, lmargin2=18,
                                rmargin=18, spacing1=4, spacing3=8)
        self.chat.tag_configure('bold', font=(FONT, 11, 'bold'))
        self.chat.tag_configure('heading', font=(DISPLAY, 15, 'bold'), spacing1=14, spacing3=8)
        self.chat.tag_configure('code', background='#0A0C10', foreground='#C9D1FF', font=(MONO, 10),
                                lmargin1=18, lmargin2=18, rmargin=18, spacing1=2, spacing3=2)

        # ---- empty state
        self.hero = tk.Frame(self.content, bg=BG)
        self.hero.grid(row=1, column=0, sticky='nsew')
        intro = tk.Frame(self.hero, bg=BG)
        intro.place(relx=.5, rely=.45, anchor='center', relwidth=1)
        if self.glyph_hero:
            tk.Label(intro, image=self.glyph_hero, bg=BG).pack(pady=(0, 18))
        tk.Label(intro, text='What are we building today?', bg=BG, fg=TEXT, font=(DISPLAY, 28, 'bold')).pack()
        sub = ('Cloud reasoning when it is available, your own machine when it is not.' if self.cloud
               else 'Private by design: everything runs on this machine.')
        tk.Label(intro, text=sub, bg=BG, fg=MUTED, font=(FONT, 11)).pack(pady=(12, 30))
        suggestions = tk.Frame(intro, bg=BG)
        suggestions.pack()
        for title, prompt in [('◇  Build a small app', 'Build a small app that '),
                              ('◇  Fix a bug', 'Find and fix the bug where '),
                              ('◇  Explain something', 'Explain this to me: ')]:
            ttk.Button(suggestions, text=title, style='Suggest.TButton',
                       command=lambda p=prompt: self.suggest(p)).pack(side='left', padx=5)

        # ---- approvals and permissions
        self.approval_card = RoundedSurface(self.content, color=SURFACE, outline=ACCENT, height=150)
        self.approval = self.approval_card  # grid/grid_remove target used by the controller
        body = self.approval_card.body
        body.columnconfigure(0, weight=1)
        self.approval_title = tk.Label(body, text='', bg=SURFACE, fg=ACCENT, font=(FONT, 8, 'bold'), anchor='w')
        self.approval_title.grid(row=0, column=0, columnspan=3, sticky='ew', pady=(2, 4))
        self.plan_label = tk.Label(body, text='', bg=SURFACE, fg=TEXT, font=(FONT, 10), justify='left',
                                   anchor='w', wraplength=700)
        self.plan_label.grid(row=1, column=0, columnspan=3, sticky='ew')
        buttons = tk.Frame(body, bg=SURFACE)
        buttons.grid(row=2, column=0, columnspan=3, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='Not now', command=lambda: self.decide(False)).pack(side='right')
        self.approve_button = ttk.Button(buttons, text='Approve', style='Accent.TButton',
                                         command=lambda: self.decide(True))
        self.approve_button.pack(side='right', padx=8)
        self.status = tk.Label(self.content, text='', bg=BG, fg=MUTED, font=(FONT, 9), anchor='w')
        self.status.grid(row=4, column=0, sticky='ew', padx=10, pady=(6, 8))

        # ---- composer
        self.compose = compose = RoundedSurface(self.content, height=132)
        compose.grid(row=5, column=0, sticky='ew')
        compose.body.columnconfigure(0, weight=1)
        compose.body.rowconfigure(0, weight=1)
        self.input = tk.Text(compose.body, height=2, bg=SURFACE, fg=TEXT, insertbackground=ACCENT, wrap='word',
                             font=(FONT, 11), relief='flat', padx=6, pady=6, highlightthickness=0, undo=True)
        self.input.grid(row=0, column=0, sticky='nsew')
        self.placeholder = tk.Label(self.input, text='Message Nessa…', bg=SURFACE, fg=FAINT, font=(FONT, 11))
        self.placeholder.place(x=6, y=6)
        self.placeholder.bind('<Button-1>', lambda _: self.input.focus_set())
        self.input.bind('<KeyRelease>', self.composer_changed)
        self.input.bind('<<Modified>>', self.composer_changed)
        self.input.bind('<Return>', self.enter)
        self.input.bind('<FocusIn>', lambda _: compose.set_outline('#3B3570'))
        self.input.bind('<FocusOut>', lambda _: compose.set_outline(BORDER))
        controls = tk.Frame(compose.body, bg=SURFACE)
        controls.grid(row=1, column=0, sticky='ew', pady=(6, 0))
        self.model_label = tk.Label(controls, text='●  Ready', bg=RAISED, fg=MUTED, font=(FONT, 8, 'bold'),
                                    padx=9, pady=4)
        self.model_label.pack(side='left', padx=4)
        self.send_button = ttk.Button(controls, text='↑', width=3, style='Accent.TButton', command=self.send)
        self.send_button.pack(side='right')
        self.stop_button = ttk.Button(controls, text='■  Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=8)
        self.stop_button.pack_forget()
        hint = 'Enter to send   ·   Shift+Enter for a new line'
        if self.enterprise:
            hint += '   ·   Nessa asks before every change'
        tk.Label(self.content, text=hint, bg=BG, fg=FAINT, font=(FONT, 8), anchor='center').grid(
            row=6, column=0, sticky='ew', pady=(10, 16))

        # ---- workspace window
        self.details = tk.Toplevel(root)
        self.details.withdraw()
        self.details.title('Nessa · Workspace')
        self.details.geometry('860x600')
        self.details.configure(bg=BG)
        if self.icons:
            self.details.iconphoto(False, *self.icons)
        self.details.protocol('WM_DELETE_WINDOW', self.details.withdraw)
        actions = ttk.Frame(self.details, padding=(18, 14, 18, 0))
        actions.pack(fill='x')
        self.apply_button = ttk.Button(actions, text='Apply to project', style='Accent.TButton',
                                       command=self.apply_changes, state='disabled')
        self.apply_button.pack(side='right')
        self.details_note = ttk.Label(actions, text='Changes stay in a private copy until you apply them.',
                                      foreground=MUTED, font=(FONT, 9))
        self.details_note.pack(side='left')
        self.tabs = ttk.Notebook(self.details)
        self.tabs.pack(fill='both', expand=True, padx=18, pady=18)
        self.activity = self.text_tab(self.tabs, 'Activity')
        self.patch = self.text_tab(self.tabs, 'Changes')
        root.bind('<Control-n>', lambda _: self.new())
        self.refresh_history()
        self.new()
        if project:
            self.root.after(100, lambda: self.create(project))
        self.tick()

    def text_panel(self, frame):
        text = tk.Text(frame, bg=BG, fg=TEXT, insertbackground=TEXT, wrap='word', font=(MONO, 10),
                       relief='flat', padx=16, pady=18, state='disabled', highlightthickness=0)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def speaker(self, name, note=''):
        """Assistant header line: the brand glyph, the name and an optional state."""
        self.chat.insert('end', '\n', 'assistant')
        if self.glyph_small:
            self.chat.image_create('end', image=self.glyph_small, padx=2)
            self.chat.insert('end', '  ', 'assistant')
        self.chat.insert('end', name, 'assistant')
        if note:
            self.chat.insert('end', '  ·  ' + note, 'working')
        self.chat.insert('end', '\n', 'assistant')

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
        self.title_label.configure(text='New conversation')
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
        self.status.configure(text='Opening the project…' if self.enterprise else 'Preparing a private project copy…')
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

    def tick(self):
        try:
            key, error = self.creation.get_nowait()
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
        except queue.Empty:
            pass
        if self.current:
            d = self.app.snapshot(self.current)
            self.project.configure(text=('▸  ' + Path(d['project']).name + ('   ·   editing in place' if self.enterprise else
                                                                            '   ·   private copy')) if d['project'] else '')
            turn = current_turn(d['activity']) if d['busy'] else []
            fingerprint = json.dumps([self.current, d['messages'], d.get('partial'), len(turn)])
            if fingerprint != self.rendered:
                if d['messages']:
                    self.hero.grid_remove()
                    self.conversation.grid()
                else:
                    self.conversation.grid_remove()
                    self.hero.grid()
                self.chat.configure(state='normal')
                self.chat.delete('1.0', 'end')
                for m in d['messages']:
                    if m['role'] == 'user':
                        self.chat.insert('end', 'YOU\n', 'user')
                        self.chat.insert('end', m['content']+'\n\n', 'user_body')
                    else:
                        self.speaker('Nessa')
                        self.render_reply(m['content'])
                    if m.get('status') not in (None, 'answered'):
                        self.chat.insert('end', m['status'].replace('_', ' ').upper()+'\n\n', 'status_meta')
                if d['busy'] and not d.get('partial') and turn:
                    # What Nessa is doing right now: real events plus the model's own reasoning.
                    self.speaker('Nessa', 'working')
                    reasoning = narrate.latest_reasoning(turn)
                    if reasoning:
                        self.chat.insert('end', reasoning + '\n', 'thinking')
                    for line in narrate.feed(turn):
                        self.chat.insert('end', line + '\n', 'meta')
                    self.chat.insert('end', '\n')
                if d.get('partial'):
                    self.speaker('Nessa')
                    self.render_reply(d['partial'])
                self.chat.configure(state='disabled')
                self.chat.see('end')
                self.rendered = fingerprint
            activity = activity_text(d['activity'])
            if self.activity.get('1.0', 'end-1c') != activity:
                self.set_text(self.activity, activity)
                self.activity.see('end')
            result = d.get('result') or {}
            patch = result.get('patch') or 'No file changes yet.'
            if result.get('evidence_dir'):
                patch = 'Evidence: '+result['evidence_dir']+'\n\n'+patch
            if self.patch.get('1.0', 'end-1c') != patch:
                self.set_text(self.patch, patch)
            applyable = (d['project'] and not d['busy'] and result.get('patch')
                         and result.get('status') in ('verified', 'unverified'))
            self.apply_button.configure(state='normal' if applyable else 'disabled')
            self.send_button.configure(state='disabled' if d['busy'] or self.creating else 'normal')
            if d['busy']:
                self.stop_button.pack(side='right', padx=8)
            else:
                self.stop_button.pack_forget()
            self.stop_button.configure(state='normal' if d['busy'] else 'disabled')
            if d['plan']:
                plan = d['plan']
                permission = str(plan.get('goal', '')).startswith('Allow ')
                color = WARN if permission else ACCENT
                self.approval_title.configure(text='PERMISSION NEEDED' if permission else 'PLAN FOR YOUR APPROVAL', fg=color)
                self.plan_label.configure(text=plan.get('goal', '') + '\n' + '\n'.join(
                    ('    ' if permission else '  •  ') + str(s) for s in plan.get('steps', [])))
                self.approve_button.configure(text='Allow' if permission else 'Approve plan',
                                              style='Warn.TButton' if permission else 'Accent.TButton')
                self.approval_card.set_outline(color)
                lines = 2 + len(plan.get('steps', [])) + sum(len(str(s)) // 90 for s in plan.get('steps', []))
                self.approval_card.configure(height=min(320, 96 + 20 * lines))
                self.approval.grid(row=3, column=0, sticky='ew', pady=(6, 4))
                status = 'Waiting for your permission' if permission else 'Waiting for your plan approval'
            else:
                self.approval.grid_remove()
                status = 'Nessa is thinking'+'.' * (1 + int(time.monotonic()) % 3)+'   '+str(int(time.monotonic()-self.started))+'s' if d['busy'] else ''
                if d['busy'] and d['activity']:
                    last_tool = next((e for e in reversed(d['activity'])
                                      if e['event'] in ('operation_started', 'operation_finished', 'model_started')), None)
                    if last_tool and last_tool['event'] == 'operation_started':
                        status = 'Using '+last_tool['data'].get('name', 'tool')+'…'
            if d['busy'] and self.app.chats[self.current].cancel.is_set():
                status = 'Stopping at the next response or tool boundary…'
            elif d['busy'] and d.get('partial'):
                status = 'Nessa is replying…'
            active = d.get('active_model', '')
            cloud_model = '/' in active
            name = active.split('/')[-1].split(':')[0] if active else ''
            self.model_label.configure(text=('●  ' + name + (' · cloud' if cloud_model else ' · on-device')) if name
                                       else '●  Ready', fg=SIGNAL if cloud_model else (OK if name else MUTED))
            switched = next((e for e in reversed(d['activity']) if e['event'] == 'model_switched'), None)
            if switched and not cloud_model and self.cloud:
                self.connection_dot.configure(fg=WARN)
                self.connection.configure(text='Cloud unavailable · on-device')
            elif name:
                self.connection_dot.configure(fg=SIGNAL if cloud_model else OK)
                self.connection.configure(text=('Cloud · ' if cloud_model else 'On-device · ') + name)
            self.title_label.configure(text=d.get('title') or 'New conversation')
            direct = bool(d.get('project')) and self.enterprise
            self.details_note.configure(text='Nessa edits this project directly; the snapshot keeps every change '
                                        'reviewable.' if direct else 'Changes stay in a private copy until you apply them.')
            if not self.creating:
                self.status.configure(text=status)
        self.root.after(400, self.tick)

    def close(self):
        if self.creating or any(c.data['busy'] for c in self.app.chats.values()):
            messagebox.showinfo('Nessa is working', 'Use Stop and wait for the current operation to return before closing. Project copying also needs to finish.')
            return
        self.app.shutdown()
        self.root.destroy()


def current_turn(activity):
    """Events since the latest user message (each turn starts by loading conversation memory)."""
    starts = [i for i, e in enumerate(activity) if e.get('event') == 'memory_loaded']
    return activity[starts[-1]:] if starts else activity


def activity_text(activity):
    lines = []
    for entry in activity:
        line = narrate.narrate(entry.get('event', ''), entry.get('data') or {})
        reasoning = str((entry.get('data') or {}).get('reasoning') or '').strip() if entry.get('event') == 'model' else ''
        stamp = time.strftime('%H:%M:%S', time.localtime(entry.get('time', 0)))
        if reasoning:
            lines.append(f'{stamp}  Reasoning: {reasoning[:1500]}')
        if line:
            lines.append(f'{stamp}  {line}')
    return '\n'.join(lines) or 'No activity yet.'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('project', nargs='?')
    p.add_argument('--base-url', default='http://127.0.0.1:11435/v1')
    p.add_argument('--model', default='nessa-lfm:latest')
    p.add_argument('--chat-model', default='nessa-lfm:latest')
    p.add_argument('--chat-base-url', default='http://127.0.0.1:11435/v1')
    p.add_argument('--review-model', help='optional advisory reviewer; disabled by default')
    p.add_argument('--review-base-url', help='reviewer endpoint; defaults to the project model endpoint')
    p.add_argument('--profile', default='lfm-i3-12gb', help='local model budget profile, e.g. lfm-32k')
    p.add_argument('--cloud', action='store_true',
                   help='use Hugging Face cloud models first, local fallback; sends content to HF providers')
    p.add_argument('--enterprise', action='store_true',
                   help='project chats edit the project in place with per-action permission prompts')
    p.add_argument('--sessions', type=Path, default=Path.home() / '.agentharness/desktop-chats')
    args = p.parse_args()
    root = tk.Tk(className='Nessa')  # matches StartupWMClass in nessa.desktop
    args.sessions.mkdir(parents=True, exist_ok=True)
    lock = (args.sessions / '.gui.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        messagebox.showinfo('Nessa is already open', 'A Nessa window is already using these conversations.')
        root.destroy()
        return
    Window(root, App(args.sessions, args.base_url, args.model, args.chat_model, args.chat_base_url,
                     args.review_model, args.review_base_url, args.profile, args.cloud,
                     args.enterprise), args.project)
    root.mainloop()


if __name__ == '__main__':
    main()
