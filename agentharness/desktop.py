"""Threaded desktop chat controller; all GUI updates stay on the Tk main thread."""
from __future__ import annotations
import json
import threading
import time
import uuid
from pathlib import Path

from .agent import Agent, AgentConfig, RunResult
from .checks import CheckRunner, detect_checks
from .llm import ChatClient
from .memory import LessonStore
from .reviewer import Reviewer
from .profiles import PROFILES
from .project_discovery import find_project, overview
from .online import is_news_query
from .session import atomic_json
from .workspace import Workspace


class Chat:
    def __init__(self, directory, data):
        self.directory, self.data = directory, data
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.decision = threading.Event()
        self.answer = (False, '')

    def save(self):
        atomic_json(self.directory / 'chat.json', self.data)

    def event(self, name, data):
        if self.cancel.is_set() and name != 'end':
            self.cancel.clear()
            raise KeyboardInterrupt
        with self.lock:
            if name == 'model_started':
                self.data['partial'] = ''
                self.data['active_model'] = data.get('model')
                self.data['model_started_at'] = time.time()
            self.data['activity'] = (self.data['activity'] + [dict(event=name, time=time.time(), data=data)])[-80:]
            self.save()

    def delta(self, text):
        if self.cancel.is_set():
            self.cancel.clear()
            raise KeyboardInterrupt
        with self.lock:
            if not self.data.get('partial'):
                self.data['first_token_seconds'] = time.time() - self.data.get('model_started_at', time.time())
            self.data['partial'] = self.data.get('partial', '') + text

    def approve(self, plan):
        with self.lock:
            self.decision.clear()
            self.data['plan'] = plan
            self.save()
        while not self.decision.wait(.25):
            if self.cancel.is_set():
                self.cancel.clear()
                raise KeyboardInterrupt
        with self.lock:
            self.data['plan'] = None
            self.save()
        return self.answer


class App:
    def __init__(self, root, base_url, model, chat_model='nessa-chat:latest',
                 chat_base_url='http://127.0.0.1:11436/v1', review_model=None, review_base_url=None):
        self.root, self.base_url, self.model = Path(root), base_url, model
        self.chat_model = chat_model
        self.chat_base_url = chat_base_url
        self.review_model, self.review_base_url = review_model, review_base_url
        self.root.mkdir(parents=True, exist_ok=True)
        self.chats = {}
        self.lock = threading.Lock()
        for p in self.root.glob('*/chat.json'):
            d = json.loads(p.read_text())
            if d.get('busy'):
                d['messages'].append(dict(role='assistant', content='The previous session was interrupted. Send a message to continue.', status='interrupted'))
            d.update(busy=False, plan=None, partial='')
            self.chats[d['id']] = Chat(p.parent, d)

    def create(self, project=None):
        project = Path(project).expanduser().resolve() if project else None
        key = uuid.uuid4().hex
        directory = self.root / key
        if project:
            Workspace.create(project, directory)
        else:
            # Plain conversations get an empty private workspace, never the cwd.
            (directory / 'repo').mkdir(parents=True)
            (directory / 'baseline').mkdir()
        d = dict(id=key, title='New conversation', project=str(project) if project else None, messages=[],
                 activity=[], busy=False, plan=None, result=None)
        chat = Chat(directory, d)
        chat.save()
        with self.lock:
            self.chats[key] = chat
        return d

    def send(self, key, message):
        chat = self.chats[key]
        with chat.lock:
            if chat.data['busy']:
                raise ValueError('Wait for the current reply before sending another message.')
            if not isinstance(message, str) or not message.strip() or len(message) > 20000:
                raise ValueError('Enter a message between 1 and 20,000 characters.')
            chat.data['busy'] = True
            chat.data['plan'] = None
            chat.data['partial'] = ''
            chat.data['first_token_seconds'] = None
            chat.data['messages'].append(dict(role='user', content=message))
            if len(chat.data['messages']) == 1:
                chat.data['title'] = message[:55]
            chat.cancel.clear()
            chat.save()
        threading.Thread(target=self.run, args=(chat, message), daemon=True).start()

    def run(self, chat, message):
        try:
            # Resolving a named local project should not wait behind inference.
            if not chat.data['project']:
                matches = find_project(message, [Path.home() / 'Projects'])
                if len(matches) > 1:
                    answer = 'I found multiple matching projects: ' + ', '.join(str(p) for p in matches) + '. Which one should I inspect?'
                elif matches:
                    chat.data['project'] = str(matches[0])
                    chat.data['work_directory'] = str(chat.directory / 'project-work')
                    answer = overview(matches[0])
                else:
                    answer = None
                if answer:
                    with chat.lock:
                        chat.data['messages'].append(dict(role='assistant', content=answer, status='answered'))
                        chat.data['result'] = RunResult('answered', answer, '', 0, [], None).to_dict()
                    return
            profile = PROFILES['lfm-i3-12gb']
            client = ChatClient(self.base_url, self.model, max_tokens=profile['max_tokens'],
                                temperature=profile['temperature'], reasoning_effort=profile['reasoning_effort'])
            fast = ChatClient(self.chat_base_url, self.chat_model, max_tokens=512,
                              temperature=0.2, timeout=45, retries=1,
                              on_delta=None if is_news_query(message) else chat.delta)
            work_directory = Path(chat.data.get('work_directory', chat.directory))
            if not (work_directory / 'repo').exists():
                Workspace.create(Path(chat.data['project']), work_directory)
            ws = Workspace(work_directory)
            cfg = AgentConfig(conversational=True, require_approval=True,
                              tool_mode='text' if profile['text_tools'] else 'native',
                              local_roots=tuple(str(Path.home()/name) for name in ('Projects', 'Documents', 'Downloads', 'Desktop', 'Pictures')),
                              max_context_chars=profile['max_context_chars'],
                              tool_output_chars=profile['tool_output_chars'], compact_at_tokens=6144)
            resume = (work_directory / 'evidence/session.json').exists()
            task = chat.data['messages'][0]['content'] if resume else message
            lessons = LessonStore().relevant(chat.data['project'], message) if chat.data['project'] else []
            reviewer = Reviewer(ChatClient(self.review_base_url or self.base_url, self.review_model,
                                max_tokens=512)) if self.review_model else None
            result = Agent(client, ws, config=cfg, checks=CheckRunner(detect_checks(ws.repo)),
                           approver=chat.approve, on_event=chat.event, chat_client=fast,
                           lessons=lessons, reviewer=reviewer).run(
                               task, resume=resume, message=message if resume else '')
            with chat.lock:
                chat.data['result'] = result.to_dict()
                chat.data['messages'].append(dict(role='assistant', content=result.summary, status=result.status))
        except BaseException as exc:
            with chat.lock:
                chat.data['messages'].append(dict(role='assistant', content='Stopped.' if isinstance(exc, KeyboardInterrupt)
                                                  else f'Unable to complete this turn: {exc}', status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'))
        finally:
            with chat.lock:
                chat.data.update(busy=False, plan=None, partial='')
                chat.save()


    def snapshot(self, key):
        chat = self.chats[key]
        with chat.lock:
            return json.loads(json.dumps(chat.data))

    def decide(self, key, approved):
        chat = self.chats[key]
        with chat.lock:
            if chat.data['plan'] is None:
                raise ValueError('No plan is awaiting approval.')
            chat.answer = (bool(approved), '')
            chat.decision.set()

    def stop(self, key):
        self.chats[key].cancel.set()
