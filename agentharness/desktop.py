"""Threaded desktop chat controller; all GUI updates stay on the Tk main thread."""
from __future__ import annotations
import json
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

    def permit(self, name, args):
        """Per-action permission in direct mode, shown in the same approval panel as plans."""
        detail = [f'{key}: {str(value)[:400]}' for key, value in args.items()]
        allowed, feedback = self.approve(dict(goal=f'Allow {name}?', steps=detail, files=[], checks=[]))
        return allowed, feedback, False


class App:
    def __init__(self, root, base_url, model, chat_model='nessa-lfm:latest',
                 chat_base_url='http://127.0.0.1:11435/v1', review_model=None, review_base_url=None,
                 profile='lfm-i3-12gb', cloud=False, enterprise=False):
        self.root, self.base_url, self.model = Path(root), base_url, model
        self.profile, self.cloud, self.enterprise = profile, cloud, enterprise
        self.scratch_root = Path.home() / 'Projects' / 'nessa-scratch'
        self.settings_path = self.root / 'settings.json'
        self.unavailable: dict[str, str] = {}  # cloud model -> last failure reason seen this session
        try:
            saved = json.loads(self.settings_path.read_text()).get('model_choice', '')
        except (OSError, ValueError):
            saved = ''
        valid = saved and (cloud or not saved.startswith(('auto', 'cloud:')))
        self.model_choice = saved if valid else ('auto' if cloud else 'local:' + model)
        self.recorder = None
        self.cloud_token = None
        if cloud:
            from . import cloud as cloud_models
            from .distill import Recorder
            self.cloud_token = cloud_models.load_token()
            self.recorder = Recorder()
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

    def model_options(self) -> list[tuple[str, str]]:
        """(label, choice) pairs for the picker: auto, cloud models, then installed local models."""
        options = []
        if self.cloud:
            from . import cloud as cloud_models
            options.append(('Auto · cloud first, on-device fallback', 'auto'))
            for model in cloud_models.DEFAULT_MODELS:
                note = self.unavailable.get(model)
                options.append((model.split('/')[-1] + ' · cloud' + (f' ({note})' if note else ''), 'cloud:' + model))
        try:
            local = ChatClient(self.base_url, self.model, timeout=5, retries=1).models()
        except Exception:
            local = [self.model]
        options += [(m.removesuffix(':latest') + ' · on-device', 'local:' + m) for m in local if m]
        return options

    def set_model(self, choice: str):
        if choice.startswith(('auto', 'cloud:')) and not self.cloud:
            raise ValueError('Cloud models are off; start Nessa without --local to use them.')
        self.model_choice = choice
        atomic_json(self.settings_path, dict(model_choice=choice))

    def _note_failures(self, errors):
        for error in errors:
            model, _, detail = error.partition(': ')
            self.unavailable[model] = 'no credits' if ('402' in detail or 'credits' in detail) else 'unavailable'

    def _clients(self, profile, stream, chat):
        """Main and conversation clients for the selected model."""
        choice = self.model_choice
        if choice.startswith('local:'):
            name = choice[len('local:'):]
            client = ChatClient(self.base_url, name, max_tokens=profile['max_tokens'],
                                temperature=profile['temperature'], reasoning_effort=profile['reasoning_effort'])
            fast = ChatClient(self.base_url, name, max_tokens=1536, temperature=0.2, timeout=900, retries=1,
                              reasoning_effort='none', on_delta=stream)
            return client, fast
        client = ChatClient(self.base_url, self.model, max_tokens=profile['max_tokens'],
                            temperature=profile['temperature'], reasoning_effort=profile['reasoning_effort'])
        fast = ChatClient(self.chat_base_url, self.chat_model, max_tokens=1536,  # LFM thinks before replying
                          temperature=0.2, timeout=900, retries=1, reasoning_effort='none', on_delta=stream)
        if not self.cloud:
            return client, fast
        from . import cloud as cloud_models

        def switch(old, new, errors):
            self._note_failures(errors)
            chat.event('model_switched', dict(previous=old, model=new, errors=errors))
        if choice.startswith('cloud:'):  # exactly the chosen model: no silent fallback
            models, main_chain, chat_chain = (choice[len('cloud:'):],), [], []
        else:
            # Auto skips models already known to be out of credits, so each turn doesn't retry them.
            models = tuple(m for m in cloud_models.DEFAULT_MODELS if self.unavailable.get(m) != 'no credits')
            main_chain, chat_chain = [client], [fast]
            if not models:
                return client, fast
        client = cloud_models.FallbackClient(cloud_models.cloud_clients(self.cloud_token, models, max_tokens=4096)
                                             + main_chain, on_switch=switch, recorder=self.recorder, on_failure=self._note_failures)
        fast = cloud_models.FallbackClient(cloud_models.cloud_clients(self.cloud_token, models, max_tokens=1536)
                                           + chat_chain, on_switch=switch, recorder=self.recorder, on_failure=self._note_failures)
        fast.on_delta = stream
        return client, fast

    def create(self, project=None):
        project = Path(project).expanduser().resolve() if project else None
        key = uuid.uuid4().hex
        directory = self.root / key
        if project:
            (Workspace.direct if self.enterprise else Workspace.create)(project, directory)
        elif self.enterprise:
            directory.mkdir(parents=True)  # a visible scratch folder is created on first use
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

    def run(self, chat, message):
        try:
            with chat.lock:
                context = ConversationMemory(chat.directory / 'memory-cache.json').refresh(
                    chat.data['messages'], message, max_chars=1600)
            chat.event('memory_loaded', dict(messages=len(chat.data['messages']), characters=len(context)))
            # Explicit user-authored memory commands run locally, even offline.
            note = re.fullmatch(r'\s*remember\s+([\w.-]{1,80})\s*:\s*(.+)', message, re.I | re.S)
            forget = re.fullmatch(r'\s*forget\s+([\w.-]{1,80})\s*', message, re.I)
            if note or forget:
                store = LessonStore()
                scope = chat.data['project'] or '*'
                if note:
                    row = store.add(scope, note[2], source=str(chat.directory / 'chat.json'), key=note[1])
                    answer = f"Remembered {note[1]} (version {row['version']})."
                else:
                    row = store.forget(scope, forget[1])
                    answer = f'Forgot {forget[1]}.'
                chat.event('memory_updated', row)
                with chat.lock:
                    chat.data['messages'].append(dict(role='assistant', content=answer, status='answered'))
                    chat.data['result'] = RunResult('answered', answer, '', 0, [], None).to_dict()
                return
            if APPLY_COMMAND.fullmatch(message):
                with chat.lock:
                    chat.data['busy'] = False
                try:
                    self.apply(chat.data['id'])
                except ValueError as exc:
                    with chat.lock:
                        chat.data['messages'].append(dict(role='assistant', content=str(exc), status='blocked'))
                return
            studio_args = studio.quick_request(message, studio_context=chat.data.get('studio_connected', False))
            if studio_args:
                store = SessionStore(chat.directory / 'evidence')
                receipt = store.begin('studio_control', studio_args, uuid.uuid4().hex, 'control')
                chat.event('studio_started', studio_args)
                try:
                    output = studio.command(studio_args)
                except Exception as exc:
                    store.finish(receipt, str(exc), 'failed')
                    raise
                unknown = output.startswith('TIMEOUT')
                store.finish(receipt, output, 'outcome_unknown' if unknown else 'completed')
                answer = studio.describe(output)
                with chat.lock:
                    chat.data['studio_connected'] = not unknown
                    chat.data['studio_last_result'] = output[:2000]
                    status = 'blocked' if unknown else 'answered'
                    chat.data['messages'].append(dict(role='assistant', content=answer, status=status))
                    chat.data['result'] = RunResult(status, answer, '', 0, [], None).to_dict()
                chat.event('studio_result', {'output': output})
                return
            # Resolving a named local project should not wait behind inference.
            if not chat.data['project'] and not studio.production_request(message):
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
            profile = PROFILES[self.profile]
            stream = None if is_news_query(message) else chat.delta
            client, fast = self._clients(profile, stream, chat)
            work_directory = Path(chat.data.get('work_directory', chat.directory))
            if not (work_directory / 'repo').exists() and not (work_directory / 'DIRECT').exists():
                if chat.data['project']:
                    (Workspace.direct if self.enterprise else Workspace.create)(Path(chat.data['project']), work_directory)
                else:
                    # Enterprise chats without a project write real, visible files the user can run.
                    scratch = self.scratch_root / time.strftime('%Y%m%d') / chat.data['id'][:8]
                    scratch.mkdir(parents=True, exist_ok=True)
                    Workspace.direct(scratch, work_directory)
                    chat.event('scratch_folder', dict(path=str(scratch)))
            ws = Workspace.open(work_directory)
            checks = CheckRunner(detect_checks(ws.repo))
            enterprise = {}
            if ws.direct:
                enterprise = dict(permission_mode='default', completion='model', plan_first=False,
                                  finish_hooks=('tests',) if 'tests' in checks.checks else ())
            cfg = AgentConfig(conversational=True, efficient_chat=True, require_approval=True,
                              studio_context=chat.data.get('studio_last_result', ''),
                              tool_mode='text' if profile['text_tools'] else 'native',
                              local_roots=tuple(str(Path.home()/name) for name in ('Projects', 'Documents', 'Downloads', 'Desktop', 'Pictures')),
                              max_context_chars=profile['max_context_chars'],
                              tool_output_chars=profile['tool_output_chars'],
                              compact_at_tokens=profile.get('compact_at_tokens', 6144),
                              keep_dev_processes=True, archive_logs=True, **enterprise)
            resume = (work_directory / 'evidence/session.json').exists()
            task = chat.data['messages'][0]['content'] if resume else message
            lessons = LessonStore().relevant(chat.data['project'] or '*', message)
            reviewer = Reviewer(ChatClient(self.review_base_url or self.base_url, self.review_model,
                                max_tokens=512)) if self.review_model else None
            result = Agent(client, ws, config=cfg, checks=checks, asker=chat.permit,
                           approver=chat.approve, on_event=chat.event, chat_client=fast,
                           lessons=lessons, reviewer=reviewer, conversation_context=context).run(
                               task, resume=resume, message=message if resume else '')
            with chat.lock:
                chat.data['result'] = result.to_dict()
                summary = result.summary
                saved_parts = '\n\n'.join(chat.data.get('turn_parts', []))
                if saved_parts and summary.startswith(saved_parts):
                    summary = summary[len(saved_parts):].strip()
                if result.status == 'cancelled' and chat.data.get('partial'):
                    chat.data['messages'].append(dict(role='assistant', content=chat.data['partial'], status='interrupted'))
                if summary:
                    chat.data['messages'].append(dict(role='assistant', content=summary, status=result.status))
        except BaseException as exc:
            with chat.lock:
                chat.data['messages'].append(dict(role='assistant', content='Stopped.' if isinstance(exc, KeyboardInterrupt)
                                                  else f'Unable to complete this turn: {exc}', status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'))
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
            if (Path(chat.data.get('work_directory', chat.directory)) / 'DIRECT').exists():
                raise ValueError('Changes are already in the project: this conversation edits it directly.')
            if status not in APPLYABLE:
                raise ValueError(f'Only verified or unverified results can be applied (latest: {status or "none"}).')
            ws = Workspace.open(Path(chat.data.get('work_directory', chat.directory)))
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
                    DevProcessManager(Workspace.open(evidence.parent), evidence).stop_all()
                except Exception:
                    pass

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
