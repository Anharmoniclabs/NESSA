"""Threaded desktop chat controller; all GUI updates stay on the Tk main thread."""
from __future__ import annotations
import json
import logging
import threading
import time
import uuid
import re
from pathlib import Path

from .agent import Agent, AgentConfig, RunResult
from .checks import CheckRunner, detect_checks
from .dev import DevProcessManager
from .llm import ChatClient
from .memory import ConversationMemory, LessonStore
from .reviewer import Reviewer
from .profiles import PROFILES
from .project_discovery import find_project, overview
from .online import is_news_query
from .session import atomic_json
from .workspace import ToolError, Workspace
from . import studio

logger = logging.getLogger(__name__)

LOCAL_ROOT_FOLDERS = ('Projects', 'Documents', 'Downloads', 'Desktop', 'Pictures')
from .session import SessionStore


APPLYABLE = ('verified', 'unverified')
APPLY_COMMAND = re.compile(r'\s*apply( the)? (changes|patch)( to (the )?project)?[.!\s]*', re.I)


class Chat:
    def __init__(self, directory, data):
        self.directory, self.data = directory, data
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.decision = threading.Event()
        self.answer = (False, '')
        self.last_partial_save = 0.0

    def save(self):
        atomic_json(self.directory / 'chat.json', self.data)

    def event(self, name, data):
        if self.cancel.is_set() and name != 'end':
            self.cancel.clear()
            raise KeyboardInterrupt
        with self.lock:
            if name == 'model_started':
                self.data['partial'] = ''
                self.data['stream_message_count'] = data.get('message_count')
                self.data['active_model'] = data.get('model')
                self.data['model_started_at'] = time.time()
            elif name == 'chat_part':
                self.data['messages'].append(dict(role='assistant', content=data['content'], status='answered'))
                self.data.setdefault('turn_parts', []).append(data['content'])
                self.data['partial'] = ''
            self.data['activity'] = (self.data['activity'] + [dict(event=name, time=time.time(), data=data)])[-80:]
            if name == 'tool' and data.get('name') == 'studio_control':
                output = data.get('output', '')
                if output.startswith('{'):
                    self.data['studio_connected'] = True
                    self.data['studio_last_result'] = output[:2000]
            self.save()

    def delta(self, text):
        if self.cancel.is_set():
            self.cancel.clear()
            raise KeyboardInterrupt
        with self.lock:
            if not self.data.get('partial'):
                self.data['first_token_seconds'] = time.time() - self.data.get('model_started_at', time.time())
            self.data['partial'] = self.data.get('partial', '') + text
            if time.monotonic() - self.last_partial_save >= 1:
                self.save()
                self.last_partial_save = time.monotonic()

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
    def __init__(self, root, base_url, model, chat_model='nessa-lfm:latest',
                 chat_base_url='http://127.0.0.1:11435/v1', review_model=None, review_base_url=None):
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
                partial = d.get('partial', '')
                if partial:
                    d['messages'].append(dict(role='assistant', content=partial, status='interrupted'))
                    work = Path(d.get('work_directory', p.parent))
                    state_path = work / 'evidence/session.json'
                    if state_path.exists():
                        state = json.loads(state_path.read_text())
                        # Commit only a stream whose assistant message was not saved yet.
                        if state.get('phase') == 'chat' and len(state['messages']) == d.get('stream_message_count'):
                            state['messages'].append(dict(role='assistant', content=partial))
                            state['chat_progress'] = dict(request=state['task'], tail=partial[-2000:], pending=True)
                            atomic_json(state_path, state)
                d['messages'].append(dict(role='assistant', content='The previous session was interrupted. Send a message to continue.', status='interrupted'))
            d.update(busy=False, plan=None, partial='')
            self.chats[d['id']] = Chat(p.parent, d)
            self.chats[d['id']].save()

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
            chat.data['turn_parts'] = []
            chat.data['first_token_seconds'] = None
            chat.data['messages'].append(dict(role='user', content=message))
            if len(chat.data['messages']) == 1:
                chat.data['title'] = message[:55]
            chat.cancel.clear()
            chat.save()
        threading.Thread(target=self.run, args=(chat, message), daemon=True).start()

    @staticmethod
    def _answer(chat, content: str, status: str = 'answered', *, result: bool = True) -> None:
        """Append an assistant message; optionally record it as the turn's result."""
        with chat.lock:
            chat.data['messages'].append(dict(role='assistant', content=content, status=status))
            if result:
                chat.data['result'] = RunResult(status, content, '', 0, [], None).to_dict()

    def _memory_command(self, chat, message: str) -> bool:
        """Explicit 'remember k: v' / 'forget k' commands run locally, even offline."""
        note = re.fullmatch(r'\s*remember\s+([\w.-]{1,80})\s*:\s*(.+)', message, re.I | re.S)
        forget = re.fullmatch(r'\s*forget\s+([\w.-]{1,80})\s*', message, re.I)
        if not (note or forget):
            return False
        store = LessonStore()
        scope = chat.data['project'] or '*'
        if note:
            row = store.add(scope, note[2], source=str(chat.directory / 'chat.json'), key=note[1])
            answer = f"Remembered {note[1]} (version {row['version']})."
        else:
            row = store.forget(scope, forget[1])
            answer = f'Forgot {forget[1]}.'
        chat.event('memory_updated', row)
        self._answer(chat, answer)
        return True

    def _apply_command(self, chat, message: str) -> bool:
        if not APPLY_COMMAND.fullmatch(message):
            return False
        with chat.lock:
            chat.data['busy'] = False
        try:
            self.apply(chat.data['id'])
        except ValueError as exc:
            self._answer(chat, str(exc), 'blocked', result=False)
        return True

    def _studio_command(self, chat, message: str) -> bool:
        """Run a direct studio request under an operation receipt; a timeout is an unknown outcome."""
        studio_args = studio.quick_request(message, studio_context=chat.data.get('studio_connected', False))
        if not studio_args:
            return False
        store = SessionStore(chat.directory / 'evidence')
        receipt = store.begin('studio_control', studio_args, uuid.uuid4().hex, 'control')
        chat.event('studio_started', studio_args)
        try:
            output = studio.command(studio_args)
        except Exception as exc:  # recorded as failed on the receipt, then surfaced by run()
            store.finish(receipt, str(exc), 'failed')
            raise
        unknown = output.startswith('TIMEOUT')
        store.finish(receipt, output, 'outcome_unknown' if unknown else 'completed')
        with chat.lock:
            chat.data['studio_connected'] = not unknown
            chat.data['studio_last_result'] = output[:2000]
        self._answer(chat, studio.describe(output), 'blocked' if unknown else 'answered')
        chat.event('studio_result', {'output': output})
        return True

    def _attach_named_project(self, chat, message: str) -> bool:
        """Resolve a project named in the message without waiting behind inference."""
        if chat.data['project'] or studio.production_request(message):
            return False
        matches = find_project(message, [Path.home() / 'Projects'])
        if len(matches) > 1:
            self._answer(chat, 'I found multiple matching projects: ' + ', '.join(str(p) for p in matches)
                         + '. Which one should I inspect?')
            return True
        if not matches:
            return False
        chat.data['project'] = str(matches[0])
        chat.data['work_directory'] = str(chat.directory / 'project-work')
        self._answer(chat, overview(matches[0]))
        return True

    def _run_agent(self, chat, message: str, context: str) -> None:
        profile = PROFILES['lfm-i3-12gb']
        client = ChatClient(self.base_url, self.model, max_tokens=profile['max_tokens'],
                            temperature=profile['temperature'], reasoning_effort=profile['reasoning_effort'])
        fast = ChatClient(self.chat_base_url, self.chat_model, max_tokens=640,
                          temperature=0.2, timeout=180, retries=1, reasoning_effort='none',
                          on_delta=None if is_news_query(message) else chat.delta)
        work_directory = Path(chat.data.get('work_directory', chat.directory))
        if not (work_directory / 'repo').exists():
            Workspace.create(Path(chat.data['project']), work_directory)
        ws = Workspace(work_directory)
        cfg = AgentConfig(conversational=True, efficient_chat=True, require_approval=True,
                          studio_context=chat.data.get('studio_last_result', ''),
                          tool_mode='text' if profile['text_tools'] else 'native',
                          local_roots=tuple(str(Path.home() / name) for name in LOCAL_ROOT_FOLDERS),
                          max_context_chars=profile['max_context_chars'],
                          tool_output_chars=profile['tool_output_chars'], compact_at_tokens=6144,
                          keep_dev_processes=True)
        resume = (work_directory / 'evidence/session.json').exists()
        task = chat.data['messages'][0]['content'] if resume else message
        lessons = LessonStore().relevant(chat.data['project'] or '*', message)
        reviewer = Reviewer(ChatClient(self.review_base_url or self.base_url, self.review_model,
                                       max_tokens=512)) if self.review_model else None
        result = Agent(client, ws, config=cfg, checks=CheckRunner(detect_checks(ws.repo)),
                       approver=chat.approve, on_event=chat.event, chat_client=fast,
                       lessons=lessons, reviewer=reviewer, conversation_context=context).run(
                           task, resume=resume, message=message if resume else '')
        self._record_result(chat, result)

    @staticmethod
    def _record_result(chat, result: RunResult) -> None:
        with chat.lock:
            chat.data['result'] = result.to_dict()
            summary = result.summary
            saved_parts = '\n\n'.join(chat.data.get('turn_parts', []))
            if saved_parts and summary.startswith(saved_parts):
                summary = summary[len(saved_parts):].strip()
            if result.status == 'cancelled' and chat.data.get('partial'):
                chat.data['messages'].append(dict(role='assistant', content=chat.data['partial'],
                                                  status='interrupted'))
            if summary:
                chat.data['messages'].append(dict(role='assistant', content=summary, status=result.status))

    def run(self, chat, message):
        try:
            with chat.lock:
                context = ConversationMemory(chat.directory / 'memory-cache.json').refresh(
                    chat.data['messages'], message, max_chars=1600)
            chat.event('memory_loaded', dict(messages=len(chat.data['messages']), characters=len(context)))
            local_paths = (self._memory_command, self._apply_command, self._studio_command,
                           self._attach_named_project)
            if any(handle(chat, message) for handle in local_paths):
                return
            self._run_agent(chat, message, context)
        except BaseException as exc:  # the UI must always get a final message, including on Ctrl-C
            interrupted = isinstance(exc, KeyboardInterrupt)
            self._answer(chat, 'Stopped.' if interrupted else f'Unable to complete this turn: {exc}',
                         'cancelled' if interrupted else 'error', result=False)
        finally:
            with chat.lock:
                chat.data.update(busy=False, plan=None, partial='')
                chat.save()

    def apply(self, key):
        """Write the latest accepted private changes into the real project."""
        chat = self.chats[key]
        with chat.lock:
            if chat.data['busy']:
                raise ValueError('Wait for the current reply before applying changes.')
            if not chat.data.get('project'):
                raise ValueError('This conversation has no attached project.')
            status = (chat.data.get('result') or {}).get('status')
            if status not in APPLYABLE:
                raise ValueError(f'Only verified or unverified results can be applied (latest: {status or "none"}).')
            ws = Workspace(Path(chat.data.get('work_directory', chat.directory)))
            try:
                changed = ws.apply_to(Path(chat.data['project']))
            except ToolError as exc:
                raise ValueError(str(exc)) from exc
            if not changed:
                raise ValueError('There are no unapplied changes.')
            answer = 'Applied to ' + chat.data['project'] + ': ' + ', '.join(changed)
            chat.data['messages'].append(dict(role='assistant', content=answer, status='answered'))
            chat.data['result'] = dict(chat.data['result'], patch='', applied=changed)
            chat.data['activity'] = (chat.data['activity'] + [dict(event='patch_applied', time=time.time(),
                                                                    data=dict(files=changed))])[-80:]
            chat.save()
            return changed

    def shutdown(self):
        """Stop dev processes that conversations kept running between turns."""
        for chat in list(self.chats.values()):
            evidence = Path(chat.data.get('work_directory', chat.directory)) / 'evidence'
            if (evidence / 'dev' / 'processes.json').exists():
                try:
                    DevProcessManager(Workspace(evidence.parent), evidence).stop_all()
                except Exception as exc:  # closing the window must not hang on one conversation
                    logger.warning("dev process cleanup failed for %s: %s", evidence, exc)

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
