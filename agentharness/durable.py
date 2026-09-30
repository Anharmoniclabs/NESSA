"""Opt-in bounded resume for one approved Python source file.

The existing three-stage CLI is unchanged. See docs/DURABLE_RESUME.md for the
supported boundaries and the deliberately limited consistency/sandbox guarantees.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from .agent import RunResult, clip
from .checks import CheckResult, CheckRunner, classify, parse_counts, syntax_check
from .code_proposals import MAX_SOURCE_CHARS, extract_source, interface, parse_source, validate_interface
from .llm import ChatClient, LOCAL_HOSTS, ModelError
from .three_stage import AtomicWorkspace, READ_TOOLS, StageBudgetExceeded, ThreeStageAgent, ThreeStageConfig
from .workspace import COPY_IGNORE, JUNK_SUFFIXES, SCAN_SKIP, ToolError, sha

SCHEMA = 'nessa-durable-code-only-v1'
MAX_STATE_BYTES = 16 * 1024 * 1024
NEXT_STEPS = {'intake', 'model', 'model_pending', 'model_received', 'edit_pending',
              'checks', 'checks_pending', 'checks_recorded', 'respond', 'terminal', 'cancelled'}


class ResumeError(ValueError):
    """No action is safe under the persisted contract."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.durable-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def regular_bytes(path, limit=MAX_STATE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ResumeError(f'Missing, linked, oversized or non-regular state file: {path.name}')
    return path.read_bytes()


def tree_manifest(root):
    """Hash supported source trees, including modes; refuse hidden scope gaps."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ResumeError('Source/workspace root is missing or a symlink.')
    result = {}
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in COPY_IGNORE)
        for name in dirs:
            path = Path(parent) / name
            if path.is_symlink() or name in SCAN_SKIP or name.endswith('.egg-info'):
                raise ResumeError(f'Unsupported linked or excluded source directory: {path.relative_to(root)}')
        for name in sorted(names):
            path = Path(parent) / name
            if name in COPY_IGNORE:
                continue
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode) or name.endswith(JUNK_SUFFIXES):
                raise ResumeError(f'Unsupported linked, special or excluded source file: {path.relative_to(root)}')
            result[path.relative_to(root).as_posix()] = {
                'sha256': digest(path.read_bytes()), 'mode': stat.S_IMODE(path.stat().st_mode)}
            if len(result) > 20000:
                raise ResumeError('Durable mode supports at most 20000 source files.')
    return result


class StateStore:
    """Immutable generations plus one atomic selector; never guess a newer state."""
    def __init__(self, work):
        self.work = Path(work).absolute()
        if any(p.is_symlink() for p in (self.work, *self.work.parents)):
            raise ResumeError('A durable work path must not contain symlinks.')
        self.directory = self.work / 'session'
        self.pointer = self.directory / 'CURRENT.json'
        if self.directory.is_symlink():
            raise ResumeError('Session directory must not be a symlink.')

    @contextmanager
    def locked(self):
        if os.name != 'posix':
            raise ResumeError('Durable v1 requires POSIX advisory locks.')
        import fcntl
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / 'LOCK', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ResumeError('Another durable operation is active for this run.') from None
            yield
        finally:
            os.close(fd)

    def load(self):
        try:
            pointer = json.loads(regular_bytes(self.pointer))
            generation = pointer['generation']
            if type(generation) is not int or generation < 1:
                raise ValueError('invalid generation')
            name = f'state-{generation:08d}.json'
            if pointer['file'] != name:
                raise ValueError('invalid generation filename')
            raw = regular_bytes(self.directory / name)
            if digest(raw) != pointer['sha256']:
                raise ValueError('generation checksum mismatch')
            state = json.loads(raw)
            if state['schema'] != SCHEMA or state['generation'] != generation or state['next_step'] not in NEXT_STEPS:
                raise ValueError('unsupported schema or transition')
            validate_state(state)
            return state
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ResumeError(f'Cannot load durable state: {exc}') from None

    def save(self, state):
        state = copy.deepcopy(state)
        self.directory.mkdir(parents=True, exist_ok=True)
        generation = state.get('generation', 0) + 1
        while (self.directory / f'state-{generation:08d}.json').exists():
            generation += 1  # retain orphan evidence, never promote it
        state['generation'] = generation
        raw = encoded(state)
        if len(raw) > MAX_STATE_BYTES:
            raise ResumeError('Durable state exceeds its size bound.')
        name = f'state-{generation:08d}.json'
        path = self.directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        atomic_write(self.pointer, encoded({'generation': generation, 'file': name, 'sha256': digest(raw)}))
        return state


class AppendLog:
    """Observational only. State generations, not log prose, authorize recovery."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if self.path.exists():
            try:
                for line in regular_bytes(self.path).splitlines():
                    item = json.loads(line)
                    if item['seq'] != self.seq + 1:
                        raise ValueError('noncontiguous sequence')
                    self.seq += 1
            except (ValueError, KeyError, TypeError):
                raise ResumeError('Event evidence is incomplete; refusing to truncate or overwrite it.') from None

    def __call__(self, event, **data):
        self.seq += 1
        with self.path.open('ab') as out:
            out.write(encoded({'seq': self.seq, 't': time.time(), 'event': event, **data}) + b'\n')
            out.flush()
            os.fsync(out.fileno())


def check_contract(test_runner='unittest', timeout=30):
    if (test_runner not in ('unittest', 'pytest') or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ResumeError('Choose unittest or pytest with a finite positive check timeout.')
    args = ['discover', '-q'] if test_runner == 'unittest' else ['-q', '-p', 'no:cacheprovider']
    return {'syntax': 'builtin:syntax_check', 'tests': [sys.executable, '-m', test_runner, *args],
            'timeout': timeout}


def runtime_contract(client, checks=None):
    """Only explicit nonsecret knobs; never serialize client attributes or env wholesale."""
    if (not isinstance(client.base_url, str) or not isinstance(client.model, str) or not client.model.strip() or
            type(client.max_tokens) is not int or client.max_tokens < 1 or
            type(client.temperature) not in (int, float) or not math.isfinite(client.temperature) or client.temperature < 0):
        raise ResumeError('Invalid model identity or generation configuration.')
    url = urllib.parse.urlsplit(client.base_url)
    if (url.scheme != 'http' or url.hostname not in LOCAL_HOSTS or url.username or url.password
            or url.query or url.fragment or getattr(client, 'api_key', 'local') != 'local'):
        raise ResumeError('Durable v1 requires a credential-free loopback HTTP model endpoint.')
    if type(getattr(client, 'retries', 1)) is not int or getattr(client, 'retries', 1) != 1:
        raise ResumeError('Durable model calls require retries=1; unknown responses are not silently retried.')
    if type(client.timeout) not in (int, float) or not math.isfinite(client.timeout) or client.timeout <= 0:
        raise ResumeError('Model timeout must be finite and positive.')
    model = {'class': type(client).__module__ + '.' + type(client).__qualname__,
             'base_url': client.base_url, 'model': client.model,
             'max_tokens': client.max_tokens, 'timeout': client.timeout,
             'temperature': client.temperature, 'retries': 1}
    if hasattr(client, 'think'):
        model.update(think=client.think, options=client.options, metadata=client.metadata)
    package = Path(__file__).parent
    engine = {name: digest((package / name).read_bytes()) for name in (
        'durable.py', 'three_stage.py', 'agent.py', 'checks.py', 'workspace.py',
        'code_proposals.py', 'context.py', 'tools.py', 'llm.py', 'ollama.py')}
    return {'model': model, 'checks': checks or check_contract(), 'engine': engine,
            'python': {'executable': sys.executable, 'version': sys.version},
            'tools': {'intake': list(READ_TOOLS) + ['propose_plan'],
                      'solve': ['one_approved_python_full_source_replace'], 'respond': []}}


class FixedChecks(CheckRunner):
    """Only two predefined recipes. No model-selected command or arbitrary shell."""
    def __init__(self, spec):
        if (not isinstance(spec, dict) or set(spec) != {'syntax', 'tests', 'timeout'} or
                not isinstance(spec['tests'], list) or len(spec['tests']) < 3 or
                any(not isinstance(arg, str) for arg in spec['tests'])):
            raise ResumeError('Malformed registered check definition.')
        runner = spec['tests'][2]
        if spec != check_contract(runner, spec['timeout']):
            raise ResumeError('Unsupported or changed registered check definition.')
        self.spec = spec
        super().__init__({'syntax': syntax_check, 'tests': self.tests}, timeout=spec['timeout'])

    def tests(self, ws):
        started = time.monotonic()
        timed_out = False
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(self.spec['tests'], cwd=ws.repo, shell=False,
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'})
            try:
                try:
                    proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            size = output.seek(0, 2)
            output.seek(max(0, size - 8000))
            text = output.read().decode('utf-8', 'replace')
        code = None if timed_out else proc.returncode
        command = ' '.join(self.spec['tests'])
        status, counts = classify(command, code, text, timed_out), parse_counts(text)
        if self.spec['tests'][2] == 'unittest':
            skipped = re.findall(r'\bskipped=(\d+)', text)
            counts['skipped'] = int(skipped[-1]) if skipped else 0
            counts['passed'] = max(0, counts.get('passed', 0) - counts['skipped'])
        if status == 'passed' and counts.get('passed', 0) < 1:
            status = 'no_tests'
            text += '\nNo executed passing test was reported; a zero exit code alone is not verification.'
        return CheckResult('tests', status, command,
            code, time.monotonic() - started, counts, text)


def validate_config(data):
    if set(data) != {f.name for f in fields(ThreeStageConfig)}:
        raise ResumeError('Incomplete or unknown configuration; defaults are never filled on resume.')
    cfg = ThreeStageConfig(**data)
    for name, default in asdict(ThreeStageConfig()).items():
        value = data[name]
        if isinstance(default, tuple):
            valid = isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value)
        elif name == 'time_budget' or type(default) is float:
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = type(value) is type(default)
        if not valid:
            raise ResumeError(f'Invalid configuration field type: {name}')
    if not 8000 <= cfg.context_chars <= 64000 or not 32 <= cfg.respond_tokens <= 1024:
        raise ResumeError('Unsupported context or response bound.')
    for name in ('max_steps', 'plan_steps', 'max_proposals'):
        if type(getattr(cfg, name)) is not int or not 1 <= getattr(cfg, name) <= 10000:
            raise ResumeError(f'{name} must be an integer in 1..10000.')
    if (type(cfg.time_budget) not in (int, float) or not math.isfinite(cfg.time_budget)
            or cfg.time_budget <= 0):
        raise ResumeError('time_budget must be finite and positive.')
    for name in ('require_approval', 'plan_first', 'allow_shell', 'allow_extract'):
        if type(getattr(cfg, name)) is not bool:
            raise ResumeError(f'{name} must be an explicit boolean.')
    if (cfg.solve_mode != 'code-only' or cfg.allow_shell or cfg.allow_extract or not cfg.plan_first
            or list(cfg.verify) != ['syntax', 'tests']):
        raise ResumeError('Durable v1 requires code-only, plan-first, syntax/tests, and no shell/extraction tools.')
    return cfg


def validate_state(state):
    """Reject incomplete/cross-field-inconsistent receipts, never backfill authority."""
    required = {'schema', 'generation', 'source', 'work', 'task', 'config', 'runtime',
                'hashes', 'approval', 'stage', 'next_step', 'steps', 'proposals',
                'active_seconds', 'baseline', 'final', 'journal', 'receipts',
                'feedback', 'pending', 'result', 'check_manifest'}
    if not required <= state.keys():
        raise ResumeError('Incomplete durable state.')
    cfg = validate_config(state['config'])
    for field in ('source', 'work', 'task', 'feedback', 'stage'):
        if not isinstance(state[field], str):
            raise ResumeError(f'Invalid state field: {field}')
    if not Path(state['source']).is_absolute() or not Path(state['work']).is_absolute():
        raise ResumeError('Saved source and work paths must be absolute.')
    for field in ('steps', 'proposals'):
        if type(state[field]) is not int or not 0 <= state[field] <= min(cfg.max_steps, cfg.max_proposals):
            raise ResumeError(f'Invalid persisted budget: {field}')
    if not 0 <= state['proposals'] <= state['steps']:
        raise ResumeError('Applied proposals exceed source attempts.')
    if (type(state['active_seconds']) not in (int, float) or
            not math.isfinite(state['active_seconds']) or state['active_seconds'] < 0):
        raise ResumeError('Invalid active-time accounting.')
    approval = state['approval']
    if not isinstance(approval, dict) or type(approval.get('approved')) is not bool:
        raise ResumeError('Missing explicit approval state.')
    approved = approval['approved']
    if approved:
        plan = approval['plan']
        if (not isinstance(plan, dict) or set(plan) != {'goal', 'steps', 'files', 'checks'} or
                not isinstance(plan['files'], list) or len(plan['files']) != 1 or
                not isinstance(plan['files'][0], str) or
                not isinstance(plan['checks'], list) or not plan['checks'] or
                any(n not in ('syntax', 'tests') for n in plan['checks'])):
            raise ResumeError('Invalid single-file approved plan/check scope.')
        path = plan['files'][0]
        kind = 'callback' if cfg.require_approval else 'explicit_auto_approve'
        if approval.get('kind') != kind:
            raise ResumeError('Approval mode does not match the saved config.')
    elif approval.get('plan') is not None:
        raise ResumeError('Unapproved plan cannot be restored as authority.')
    if state['next_step'] not in ('intake', 'terminal', 'cancelled') and not approved:
        raise ResumeError('No durable approval for this continuation.')
    hashes = state['hashes']
    if not isinstance(hashes, dict) or set(hashes) != {'source', 'baseline', 'current'}:
        raise ResumeError('Missing source/baseline/current manifests.')
    for manifest in hashes.values():
        if not isinstance(manifest, dict):
            raise ResumeError('Invalid manifest.')
        for name, value in manifest.items():
            if (not isinstance(name, str) or not isinstance(value, dict) or
                    set(value) != {'sha256', 'mode'} or not isinstance(value['sha256'], str) or
                    len(value['sha256']) != 64 or any(c not in '0123456789abcdef' for c in value['sha256']) or
                    type(value['mode']) is not int or not 0 <= value['mode'] <= 0o7777):
                raise ResumeError('Invalid file hash or mode.')
    if hashes['source'] != hashes['baseline'] or hashes['current'].keys() != hashes['baseline'].keys():
        raise ResumeError('Snapshot identity/scope changed.')
    if not isinstance(state['journal'], list) or len(state['journal']) != state['proposals']:
        raise ResumeError('Edit receipt count does not match proposal count.')
    if not isinstance(state['receipts'], list):
        raise ResumeError('Missing operation receipts.')
    if approved:
        if path not in hashes['baseline']:
            raise ResumeError('Approved file is absent from snapshot.')
        expected = hashes['baseline'][path]['sha256'][:12]
        for entry in state['journal']:
            if (entry['op'] != 'replace' or entry['path'] != path or entry['before'] != expected
                    or not isinstance(entry['after'], str) or len(entry['after']) != 12):
                raise ResumeError('Inconsistent edit journal chain.')
            expected = entry['after']
        if (hashes['current'][path]['sha256'][:12] != expected or
                hashes['current'][path]['mode'] != hashes['baseline'][path]['mode']):
            raise ResumeError('Current state does not match edit receipts.')
        if any(value != hashes['baseline'][name] for name, value in hashes['current'].items() if name != path):
            raise ResumeError('Unapproved source change in persisted state.')
    elif state['journal'] or hashes['current'] != hashes['baseline']:
        raise ResumeError('Unapproved state contains edits.')
    runtime = state['runtime']
    if not isinstance(runtime, dict) or set(runtime) != {'model', 'checks', 'engine', 'python', 'tools'}:
        raise ResumeError('Incomplete runtime/tool contract.')
    model_keys = {'class', 'base_url', 'model', 'max_tokens', 'timeout', 'temperature', 'retries'}
    model = runtime['model']
    if not isinstance(model, dict) or not model_keys <= model.keys():
        raise ResumeError('Incomplete model generation/transport configuration.')
    if (any(not isinstance(model[n], str) or not model[n].strip() for n in ('class', 'base_url', 'model')) or
            type(model['temperature']) not in (int, float) or not math.isfinite(model['temperature']) or model['temperature'] < 0 or
            type(model['timeout']) not in (int, float) or not math.isfinite(model['timeout']) or model['timeout'] <= 0 or
            type(model['max_tokens']) is not int or model['max_tokens'] < 1 or
            type(model['retries']) is not int or model['retries'] != 1):
        raise ResumeError('Invalid persisted model identity/generation budgets.')
    extra = {'think', 'options', 'metadata'} if model['class'] == 'agentharness.ollama.OllamaClient' else set()
    if set(model) != model_keys | extra:
        raise ResumeError('Unexpected or incomplete model transport configuration.')
    FixedChecks(runtime['checks'])  # validate argv allowlist without executing it
    if runtime['tools'] != {'intake': list(READ_TOOLS) + ['propose_plan'],
                           'solve': ['one_approved_python_full_source_replace'], 'respond': []}:
        raise ResumeError('Persisted tool scope is unsupported.')
    for checks in (state['baseline'], state['final']):
        if not isinstance(checks, dict) or any(name not in ('syntax', 'tests') for name in checks):
            raise ResumeError('Invalid check receipt scope.')
        for name, receipt in checks.items():
            if not isinstance(receipt, dict) or set(receipt) != {f.name for f in fields(CheckResult)}:
                raise ResumeError('Incomplete check receipt.')
            result = CheckResult(**receipt)
            if (not isinstance(result.command, str) or not isinstance(result.output, str) or
                    not (result.exit_code is None or type(result.exit_code) is int) or
                    type(result.seconds) not in (int, float) or not math.isfinite(result.seconds) or result.seconds < 0 or
                    not isinstance(result.counts, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0
                                                               for k, v in result.counts.items())):
                raise ResumeError('Malformed check receipt field types.')
            if result.name != name or result.status not in ('passed', 'failed', 'timeout', 'setup_error', 'no_tests', 'error'):
                raise ResumeError('Invalid check receipt.')
            expected_command = 'compile changed files' if name == 'syntax' else ' '.join(runtime['checks']['tests'])
            if result.command not in ('', expected_command) or (result.status == 'passed' and result.command != expected_command):
                raise ResumeError('Check receipt command differs from its pinned registered recipe.')
            if result.status == 'passed' and (result.exit_code != 0 or
                    any(result.counts.get(key, 0) for key in ('failed', 'error', 'errors')) or
                    (name == 'tests' and result.counts.get('passed', 0) < 1) or
                    (name == 'syntax' and result.counts.get('files', 0) < 1)):
                raise ResumeError('Passing check receipt conflicts with its exit code or executed-test counts.')
    phase, pending = state['next_step'], state['pending']
    if phase in ('model_pending', 'checks_pending'):
        if (not isinstance(pending, dict) or type(pending.get('reserved_seconds')) not in (int, float)
                or not math.isfinite(pending['reserved_seconds']) or pending['reserved_seconds'] <= 0):
            raise ResumeError('Missing finite pending-operation reservation.')
        expected = model['timeout'] if phase == 'model_pending' else runtime['checks']['timeout'] * 2
        if pending['reserved_seconds'] != expected:
            raise ResumeError('Pending reservation does not match configured operation limits.')
    if phase in ('model_pending', 'model_received', 'edit_pending'):
        if not 0 <= state['proposals'] < state['steps']:
            raise ResumeError('Pending source operation lacks a charged attempt.')
        if not isinstance(pending, dict) or pending.get('before_sha256') != hashes['current'][path]['sha256']:
            raise ResumeError('Pending operation preimage is inconsistent.')
    if phase == 'model_received':
        reply = pending['reply']
        if (not isinstance(reply, dict) or set(reply) != {'content', 'native', 'finish_reason'} or
                not isinstance(reply['content'], str) or len(reply['content']) > 24000 or
                type(reply['native']) is not bool or
                not (reply['finish_reason'] is None or isinstance(reply['finish_reason'], str))):
            raise ResumeError('Invalid durable response receipt.')
    if phase == 'edit_pending':
        if (set(pending) != {'path', 'source', 'before_sha256', 'journal_entry'} or
                pending['path'] != path or not isinstance(pending['source'], str)):
            raise ResumeError('Invalid pending replacement scope.')
        entry = pending['journal_entry']
        if (set(entry) != {'t', 'op', 'path', 'before', 'after'} or entry['op'] != 'replace'
                or entry['path'] != path or entry['before'] != pending['before_sha256'][:12]
                or entry['after'] != sha(pending['source']) or
                type(entry['t']) not in (int, float) or not math.isfinite(entry['t'])):
            raise ResumeError('Pending edit journal does not match the proposal.')
    if phase in ('model', 'checks', 'checks_recorded', 'respond', 'terminal') and pending is not None:
        raise ResumeError('Unexpected pending operation for this boundary.')
    changed = sorted(name for name, value in hashes['current'].items() if value != hashes['baseline'][name])
    if state['final']:
        if set(state['final']) != {'syntax', 'tests'} or state['check_manifest'] != hashes['current']:
            raise ResumeError('Final checks are incomplete or do not belong to the current source.')
    elif state['check_manifest'] is not None:
        raise ResumeError('A check manifest without check receipts is invalid.')
    if phase in ('checks', 'checks_pending', 'checks_recorded') and not state['journal']:
        raise ResumeError('Post-edit checks require an applied edit receipt.')
    if phase == 'checks_recorded' and not state['final']:
        raise ResumeError('Recorded-check boundary needs complete check receipts.')
    verified = (approved and changed == [path] and not ThreeStageAgent._protected(path)
                and bool(state['journal']) and set(state['final']) == {'syntax', 'tests'}
                and all(r['status'] == 'passed' for r in state['final'].values())
                and state['final']['tests']['counts'].get('passed', 0) > 0)
    if phase == 'respond' and (state.get('final_status') != 'verified' or not verified):
        raise ResumeError('Response stage cannot establish completion without verified check receipts.')
    if phase == 'terminal':
        if not isinstance(state['result'], dict) or set(state['result']) != {f.name for f in fields(RunResult)}:
            raise ResumeError('Incomplete terminal result.')
        result = RunResult(**state['result'])
        if (any(not isinstance(getattr(result, key), str) for key in ('status', 'summary', 'patch', 'evidence_dir')) or
                type(result.steps) is not int or not isinstance(result.changed_files, list) or
                any(not isinstance(n, str) for n in result.changed_files) or
                type(result.seconds) not in (int, float) or not math.isfinite(result.seconds) or result.seconds < 0):
            raise ResumeError('Malformed terminal result field types.')
        expected_checks = {key: {n: CheckResult(**r).brief() for n, r in state[key].items()}
                           for key in ('baseline', 'final')}
        if (result.status not in ('verified', 'no_change', 'failed_checks', 'no_plan', 'rejected',
                                  'budget_exhausted', 'error') or
                result.steps != state['steps'] or result.changed_files != changed or
                result.plan != approval['plan'] or result.checks != expected_checks or
                (result.status == 'verified' and not verified) or
                (result.status in ('rejected', 'no_plan') and approved) or
                (result.status == 'no_change' and changed)):
            raise ResumeError('Terminal result conflicts with approval, edits or check receipts.')


def stable_brief(result):
    return CheckResult(**json.loads(encoded(result.to_dict()))).brief()


class DurableAgent(ThreeStageAgent):
    _manifest = staticmethod(tree_manifest)

    def __init__(self, client, work, state, store, *, approver=None, boundary=None):
        cfg = validate_config(state['config'])
        super().__init__(client, AtomicWorkspace(work), config=cfg,
            checks=FixedChecks(state['runtime']['checks']),
            # A saved approval is restored below, never requested or fabricated by this callback.
            approver=approver if state['next_step'] == 'intake' else lambda plan: (False, 'Resume cannot approve plans'),
            event_log=AppendLog(Path(work) / 'evidence/events.jsonl'))
        self.state, self.store, self.boundary = state, store, boundary
        self.task = state['task']
        self.plan = state['approval']['plan'] if state['approval']['approved'] else None
        self.approved_files = set(self.plan['files']) if self.plan else set()
        self.required_checks = tuple(dict.fromkeys((*cfg.verify, *(self.plan['checks'] if self.plan else ()))))
        self.steps, self.proposals = state['steps'], state['proposals']
        self.edit_count = self.proposals
        self.ws.journal = copy.deepcopy(state['journal'])
        self.authorized_state = copy.deepcopy(state['hashes']['current'])
        self.baseline = {k: CheckResult(**v) for k, v in state['baseline'].items()}
        self.final = {k: CheckResult(**v) for k, v in state['final'].items()}
        self.feedback = state['feedback']
        self.elapsed_before = state['active_seconds']
        self.active_started = time.monotonic()
        self.approval_seconds = 0.0

    def _expired(self):
        return self.elapsed() >= self.config.time_budget

    def elapsed(self):
        return self.elapsed_before + time.monotonic() - self.active_started - self.approval_seconds

    def persist(self, next_step, boundary=None, **updates):
        self.state.update(next_step=next_step, stage=('intake' if next_step == 'intake' else
                          'respond' if next_step == 'respond' else 'terminal' if next_step == 'terminal' else 'solve'),
            steps=self.steps, proposals=self.proposals, journal=self.ws.journal,
            baseline={k: v.to_dict() for k, v in self.baseline.items()},
            final={k: v.to_dict() for k, v in self.final.items()}, feedback=self.feedback,
            active_seconds=self.elapsed())
        self.state.update(updates)
        self.state = self.store.save(self.state)
        if self.boundary:
            self.boundary(boundary or next_step, copy.deepcopy(self.state))

    def assert_integrity(self, allow_pending=False):
        s = self.state
        if tree_manifest(s['source']) != s['hashes']['source']:
            raise ResumeError('Original source changed; start a new run and obtain a fresh approval.')
        if tree_manifest(self.ws.baseline) != s['hashes']['baseline']:
            raise ResumeError('Baseline changed; durable evidence cannot be reconciled.')
        actual = tree_manifest(self.ws.repo)
        expected = s['hashes']['current']
        if actual == expected:
            return 'before'
        if allow_pending and s['next_step'] == 'edit_pending':
            post = copy.deepcopy(expected)
            proposal = s['pending']
            post[proposal['path']]['sha256'] = digest(proposal['source'].encode())
            if actual == post:
                return 'after'
        raise ResumeError('Workspace changed outside the exact pending edit; refusing recovery.')

    def prepare(self):
        if self.plan is None or not self.state['approval']['approved']:
            raise ResumeError('No durable approved plan; intake/approval must be restarted in a new run.')
        if self._validate_plan(self.plan) != self.plan or len(self.approved_files) != 1:
            raise ResumeError('Durable v1 requires exactly one approved existing Python file.')
        path = next(iter(self.approved_files))
        if not path.endswith('.py'):
            raise ResumeError('Durable v1 only supports one Python source file.')
        original = (self.ws.baseline / path).read_text(encoding='utf-8')
        required = interface(parse_source(original, path))
        if len(original) > MAX_SOURCE_CHARS:
            raise ResumeError('Approved source exceeds the code-only bound.')
        return path, required

    def source_prompt(self, path):
        system = ('You are repairing a Python source file. Return only the complete corrected Python source file. '
                  'Do not return explanations, JSON, Markdown fences, or tool calls. '
                  'Preserve every existing public function name and signature. Make only the repair requested.')
        before = self.ws._text(self.ws.path(path))
        prefix = f'Task:\n{self.task}\n\nCurrent source file {path}:\n'
        instructions = self.project_instructions.context_for(path)
        suffix = '\n\nProject instructions:\n' + instructions if instructions else ''
        header = '\n\nController feedback on the last proposal:\n'
        bound = min(MAX_SOURCE_CHARS, self.config.context_chars - len(system) - len(prefix) - len(suffix) - len(header) - 512)
        if len(before) > bound:
            raise ResumeError('Required source context exceeds the correction-safe bound.')
        body = prefix + before + suffix
        if self.feedback:
            room = self.config.context_chars - len(system) - len(body) - len(header)
            body += header + self.feedback[:min(5000, max(0, room))]
        return before, bound, [{'role': 'system', 'content': system}, {'role': 'user', 'content': body}]

    def checkpoint(self):
        path = self.evidence_dir / 'checkpoints' / f'edit-{self.proposals:04d}.diff'
        atomic_write(path, self.ws.patch().encode())

    def reconcile_edit(self):
        effect = self.assert_integrity(allow_pending=True)
        pending = self.state['pending']
        path, required = self.prepare()
        if pending['path'] != path or pending['before_sha256'] != self.state['hashes']['current'][path]['sha256']:
            raise ResumeError('Edit receipt does not match approved scope/preimage.')
        _, bound, _ = self.source_prompt(path)
        try:
            source = extract_source(pending['source'], path)
            validate_interface(source, path, required)
        except ToolError as exc:
            raise ResumeError('Invalid pending source receipt: ' + str(exc)) from None
        if source != pending['source'] or len(source) > bound:
            raise ResumeError('Pending replacement exceeds the original correction-safe source contract.')
        validate_interface(source, path, required)
        if digest(pending['source'].encode()) == pending['before_sha256']:
            raise ResumeError('Unchanged proposal cannot authorize an edit.')
        if effect == 'before':
            if self._expired():
                return self.finish('budget_exhausted')
            self.ws.read(path)
            before = self.ws._text(self.ws.path(path))
            self.ws.replace(path, before, pending['source'])
            if self.boundary:
                self.boundary('edit_replaced', copy.deepcopy(self.state))
        # Reconstruct exactly one receipt whether the replace or the receipt committed first.
        self.ws.journal = [*self.state['journal'], pending['journal_entry']]
        self.proposals = self.state['proposals'] + 1
        self.edit_count = self.proposals
        expected_post = copy.deepcopy(self.state['hashes']['current'])
        expected_post[path]['sha256'] = digest(source.encode())
        if tree_manifest(self.ws.repo) != expected_post:
            raise ResumeError('Workspace changed during replacement; unknown effects are not adopted as authority.')
        self.assert_integrity(allow_pending=True)
        self.authorized_state = expected_post
        self.state['hashes']['current'] = self.authorized_state
        self.state['receipts'].append({'step': self.steps, 'outcome': 'edit_reconciled_' + effect,
                                       'path': path, 'after_sha256': digest(pending['source'].encode())})
        self.final = {}
        self.state['check_manifest'] = None
        self.checkpoint()
        self.persist('checks', boundary='edit_committed', pending=None)
        return None

    def finish(self, status, error=''):
        changed = self.ws.changed_files()
        summary = f'Status: {status}. ' + ('Changed files: ' + ', '.join(changed) + '. ' if changed else
                                        'No repository changes were produced; no fix was applied. ')
        summary += 'Final checks: ' + ('; '.join(r.brief() for r in self.final.values()) or 'none')
        if error:
            summary += '. ' + error
        result = RunResult(status, summary, self.ws.patch(), self.steps, changed, self.plan,
            {'baseline': {k: stable_brief(v) for k, v in self.baseline.items()},
             'final': {k: stable_brief(v) for k, v in self.final.items()}}, str(self.evidence_dir), round(self.elapsed(), 3))
        if status == 'paused':
            return result
        self.persist('terminal', pending=None, result=result.to_dict(), response_stage=self.respond)
        # Terminal state already contains the full result; these are convenience exports.
        atomic_write(self.evidence_dir / 'patch.diff', result.patch.encode())
        atomic_write(self.evidence_dir / 'result.json', encoded({**result.to_dict(), 'mode': SCHEMA, 'response_stage': self.respond}))
        atomic_write(self.evidence_dir / 'journal.json', encoded(self.ws.journal))
        self.log('end', status=status, steps=self.steps, changed=changed)
        return result

    def advance(self, step_limit):
        path, required = self.prepare()
        used = 0
        self.stage = 'solve'
        while True:
            self.assert_integrity(allow_pending=True)
            phase = self.state['next_step']
            if phase in ('model_pending', 'checks_pending'):
                # Unknown model output is never replayed. Checks have no exactly-once promise.
                self.elapsed_before += self.state['pending']['reserved_seconds']
                self.state['receipts'].append({'step': self.steps, 'outcome':
                    'discarded_unknown_response' if phase == 'model_pending' else 'unknown_checks_rerun'})
                self.persist('model' if phase == 'model_pending' else 'checks', pending=None)
                continue
            if phase == 'edit_pending':
                result = self.reconcile_edit()
                if result is not None:
                    return result
                continue
            if phase == 'checks':
                if self._expired():
                    return self.finish('budget_exhausted')
                self.checkpoint()  # repair absent checkpoint only from the current edit receipt
                self.persist('checks_pending', pending={'reserved_seconds': self.checks.timeout * len(self.required_checks)})
                self.final = self._verify()
                self.persist('checks_recorded', pending=None, check_manifest=copy.deepcopy(self.authorized_state))
                continue
            if phase == 'checks_recorded':
                if self._verified():
                    self.persist('respond', final_status='verified')
                else:
                    self.feedback = 'Required checks did not pass. Repair the current source.\n' + '\n'.join(
                        r.brief() + '\n' + clip(r.output, 2000) for r in self.final.values())
                    self.persist('model')
                continue
            if phase == 'respond':
                # Narration is advisory. Interrupted narration is skipped, never retried.
                status = self.state['final_status']
                if self.state.get('response_started'):
                    self.respond = {'status': 'interrupted', 'authoritative': False}
                else:
                    self.persist('respond', response_started=True)
                    self._respond(status)
                self.assert_integrity()
                return self.finish(status)
            if phase == 'model_received':
                pending = self.state['pending']
                try:
                    before, bound, _ = self.source_prompt(path)
                    if digest(before.encode()) != pending['before_sha256']:
                        raise ResumeError('Durable response preimage changed.')
                    reply = pending['reply']
                    if reply['native'] or reply['finish_reason'] == 'length':
                        raise ToolError('Expected complete source, not tool calls or truncated output.')
                    source = extract_source(reply['content'], path)
                    if len(source) > bound or source == before:
                        raise ToolError('Source unchanged or exceeds correction-safe context bound; no edit applied.')
                    validate_interface(source, path, required)
                    entry = {'t': time.time(), 'op': 'replace', 'path': path, 'before': sha(before), 'after': sha(source)}
                    self.persist('edit_pending', pending={'path': path, 'source': source,
                        'before_sha256': digest(before.encode()), 'journal_entry': entry})
                except ToolError as exc:
                    self.feedback = 'Proposal rejected before apply: ' + str(exc)
                    self.state['receipts'].append({'step': self.steps, 'outcome': 'proposal_rejected', 'reason': str(exc)})
                    self.log('rejection', phase='solve', reason=str(exc))
                    self.persist('model', pending=None)
                continue
            if phase != 'model':
                raise ResumeError('Unsupported continuation boundary.')
            if self._expired():
                return self.finish('budget_exhausted')
            if self.steps >= min(self.config.max_steps, self.config.max_proposals):
                return self.finish('failed_checks' if self.ws.changed_files() else 'no_change')
            if used >= step_limit:
                self.persist('model')
                return self.finish('paused')
            before, _, messages = self.source_prompt(path)
            self.steps += 1
            used += 1
            self.persist('model_pending', pending={'before_sha256': digest(before.encode()),
                         'reserved_seconds': self.client.timeout})
            reply = self.client.chat(messages, None, None)
            # Bound content before persisting; discarded oversized responses cannot be applied.
            content = reply.content
            if not isinstance(content, str) or len(content) > 24000:
                content = ''
            self.persist('model_received', pending={'before_sha256': digest(before.encode()),
                'reply': {'content': content, 'native': reply.native, 'finish_reason': reply.finish_reason}})


class DurableRun:
    @staticmethod
    def start(project, work, task, *, client, config, approver=None, test_runner='unittest',
              check_timeout=30, pause_after_approval=False, boundary=None):
        validate_config(asdict(config))
        runtime = runtime_contract(client, check_contract(test_runner, check_timeout))
        source = Path(project).resolve()
        manifest = tree_manifest(source)
        work = Path(work).absolute()
        store = StateStore(work)
        if work.exists() and any(work.iterdir()):
            raise ResumeError('New durable runs require an empty/new work directory.')
        if work == source or source in work.parents:
            raise ResumeError('State/work directory must be outside the source project.')
        with store.locked():
            # A competing start may have populated the directory after the pre-lock check.
            if (set(p.name for p in work.iterdir()) != {'session'} or
                    set(p.name for p in store.directory.iterdir()) != {'LOCK'}):
                raise ResumeError('Work directory was initialized before this start acquired its lock.')
            ws = AtomicWorkspace.create(source, work)
            baseline, current = tree_manifest(ws.baseline), tree_manifest(ws.repo)
            if manifest != baseline or baseline != current or tree_manifest(source) != manifest:
                raise ResumeError('Source changed during snapshot, or snapshot scope is unsupported.')
            state = {'schema': SCHEMA, 'generation': 0, 'source': str(source), 'work': str(work),
                     'task': task, 'config': asdict(config), 'runtime': runtime,
                     'hashes': {'source': manifest, 'baseline': baseline, 'current': current},
                     'approval': {'approved': False, 'plan': None}, 'stage': 'intake', 'next_step': 'intake',
                     'steps': 0, 'proposals': 0, 'active_seconds': 0, 'baseline': {}, 'final': {},
                     'journal': [], 'receipts': [], 'feedback': '', 'pending': None, 'result': None, 'check_manifest': None}
            state = store.save(state)
            agent = DurableAgent(client, work, state, store, approver=approver, boundary=boundary)
            agent.log('start', mode=SCHEMA, task=task, config=asdict(config), model=client.model)
            agent.baseline = agent._verify('baseline')
            try:
                status = agent._intake()
            except StageBudgetExceeded as exc:
                return agent.finish('budget_exhausted', str(exc))
            if status is not None:
                return agent.finish(status)
            agent.assert_integrity()
            agent.state['approval'] = {'approved': True, 'plan': agent.plan,
                                       'kind': 'callback' if config.require_approval else 'explicit_auto_approve'}
            agent.prepare()
            agent.persist('model', boundary='approved')
            if pause_after_approval:
                return agent.finish('paused')
            return agent.advance(min(config.max_steps, config.max_proposals))

    @staticmethod
    def resume(work, *, client=None, steps, boundary=None):
        if type(steps) is not int or not 1 <= steps <= 10000:
            raise ResumeError('Resume requires an explicit --steps bound of 1..10000.')
        store = StateStore(work)
        with store.locked():
            state = store.load()
            if state['next_step'] == 'cancelled':
                raise ResumeError('Run was cancelled; it cannot resume.')
            if state['next_step'] == 'terminal':
                return RunResult(**state['result'])
            if not state['approval']['approved'] or state['next_step'] == 'intake':
                raise ResumeError('No durable approval; start a new run rather than guessing an interrupted intake.')
            if state['work'] != str(store.work):
                raise ResumeError('Work directory moved; automatic rebinding is unsupported.')
            validate_config(state['config'])
            # Validate integrity before contacting a model or writing evidence.
            if tree_manifest(state['source']) != state['hashes']['source'] or tree_manifest(store.work / 'baseline') != state['hashes']['baseline']:
                raise ResumeError('Source or baseline changed; obtain a fresh approval in a new run.')
            actual = tree_manifest(store.work / 'repo')
            accepted = [state['hashes']['current']]
            if state['next_step'] == 'edit_pending':
                post = copy.deepcopy(accepted[0])
                post[state['pending']['path']]['sha256'] = digest(state['pending']['source'].encode())
                accepted.append(post)
            if actual not in accepted:
                raise ResumeError('Workspace drift or unknown edit effect; refusing recovery.')
            client = client if client is not None else restore_client(state['runtime']['model'])
            if runtime_contract(client, state['runtime']['checks']) != state['runtime']:
                raise ResumeError('Model, transport, generation, checks, interpreter or controller changed.')
            agent = DurableAgent(client, store.work, state, store, boundary=boundary)
            return agent.advance(steps)

    @staticmethod
    def cancel(work):
        store = StateStore(work)
        with store.locked():
            state = store.load()
            if state['next_step'] not in ('terminal', 'cancelled'):
                state.update(next_step='cancelled', stage='cancelled', cancelled_at=time.time())
                store.save(state)


def restore_client(spec):
    args = {k: spec[k] for k in ('base_url', 'model', 'temperature', 'max_tokens', 'timeout')}
    if spec['class'] == 'agentharness.llm.ChatClient':
        return ChatClient(**args, retries=1)
    if spec['class'] == 'agentharness.ollama.OllamaClient':
        from .ollama import OllamaClient
        client = OllamaClient(**args, think=spec['think'], **spec['options'])
        client.discover()
        return client
    raise ResumeError('This saved model adapter cannot be restored; no fallback is allowed.')


def main(argv=None):
    from .__main__ import cli_approver
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('project')
    start.add_argument('task')
    start.add_argument('--work', required=True)
    start.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    start.add_argument('--model', default='qwen2.5-coder:1.5b')
    start.add_argument('--transport', choices=('openai', 'ollama-native'), default='openai')
    start.add_argument('--ollama-think')
    start.add_argument('--ollama-num-ctx', type=int, default=8192)
    start.add_argument('--ollama-num-thread', type=int, default=2)
    start.add_argument('--ollama-seed', type=int, default=42)
    start.add_argument('--max-tokens', type=int, default=768)
    start.add_argument('--timeout', type=float, default=180)
    start.add_argument('--max-steps', type=int, default=3)
    start.add_argument('--plan-steps', type=int, default=6)
    start.add_argument('--time-budget', type=float, default=600)
    start.add_argument('--test-runner', choices=('unittest', 'pytest'), default='unittest')
    start.add_argument('--check-timeout', type=float, default=30)
    start.add_argument('--auto-approve', action='store_true')
    start.add_argument('--text-tools', action='store_true')
    start.add_argument('--pause-after-approval', action='store_true')
    resume = commands.add_parser('resume')
    resume.add_argument('work')
    resume.add_argument('--steps', type=int, required=True)
    cancel = commands.add_parser('cancel')
    cancel.add_argument('work')
    args = parser.parse_args(argv)
    try:
        if args.command == 'cancel':
            DurableRun.cancel(args.work)
            print('Run cancelled or already terminal; no further actions will execute.')
            return 0
        if args.command == 'resume':
            result = DurableRun.resume(args.work, steps=args.steps)
        else:
            if args.transport == 'ollama-native':
                if args.ollama_think is None:
                    parser.error('--ollama-think is required for native transport')
                from .ollama import OllamaClient
                think = {'true': True, 'false': False}.get(args.ollama_think, args.ollama_think)
                client = OllamaClient(args.base_url, args.model, think=think, max_tokens=args.max_tokens,
                    timeout=args.timeout, num_ctx=args.ollama_num_ctx, num_thread=args.ollama_num_thread,
                    seed=args.ollama_seed)
                client.discover()
            else:
                if args.ollama_think is not None:
                    parser.error('--ollama-think requires native transport')
                client = ChatClient(args.base_url, args.model, max_tokens=args.max_tokens, timeout=args.timeout, retries=1)
            cfg = ThreeStageConfig(solve_mode='code-only', require_approval=not args.auto_approve,
                allow_shell=False, allow_extract=False, max_steps=args.max_steps,
                max_proposals=args.max_steps, plan_steps=args.plan_steps, time_budget=args.time_budget,
                tool_mode='text' if args.text_tools else 'native')
            result = DurableRun.start(args.project, args.work, args.task, client=client, config=cfg,
                approver=None if args.auto_approve else cli_approver, test_runner=args.test_runner,
                check_timeout=args.check_timeout, pause_after_approval=args.pause_after_approval)
        print(result.summary)
        print(f'Patch + evidence: {result.evidence_dir}')
        return 0 if result.status == 'verified' else 2 if result.status == 'paused' else 1
    except KeyboardInterrupt:
        print('Interrupted. Durable evidence retained; explicitly resume with a step bound or cancel.')
        return 130
    except (ResumeError, ModelError, ToolError, ValueError, OSError) as exc:
        print(f'Durable run stopped without broadening authority: {type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
