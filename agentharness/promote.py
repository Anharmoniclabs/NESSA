"""Score a candidate harness version outside its editable workspace and gate its promotion.

Flow:  freeze -> evaluate (baseline, candidate) -> promote

freeze     hash every task file and the scoring command's inputs into frozen.json
evaluate   verify the frozen hashes, copy the candidate into a fresh directory, run the
           scoring command once per task (exit code 0 = pass), record time and memory
promote    compare two scorecards under an explicit policy; a passing decision is only
           recorded after the caller supplies the candidate's identity hash

The candidate never gets to edit its own exam: tasks and scorer live outside the copy it
runs from, and their hashes are re-verified after every evaluation. Passing the harness's
own unit tests is deliberately not part of the decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORED = shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '.pytest_cache')


class PromotionError(Exception):
    pass


def tree_digest(root: Path) -> str:
    """Hash of every file path and content under `root` (or of one file)."""
    root = Path(root)
    digest = hashlib.sha256()
    files = [root] if root.is_file() else sorted(p for p in root.rglob('*') if p.is_file()
                                                  and '__pycache__' not in p.parts)
    for path in files:
        digest.update((path.name if root.is_file() else path.relative_to(root).as_posix()).encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass
class Frozen:
    """An immutable evaluation: tasks, the command that scores them, and hashes proving neither changed."""
    name: str
    command: str                      # run once per task; exit code 0 means the task passed
    tasks: dict[str, str]             # task id -> task directory (absolute)
    digests: dict[str, str]           # task id -> digest; "scorer" -> digest of scorer paths
    scorer_paths: list[str] = field(default_factory=list)
    timeout: float = 600
    repeats: int = 1                  # a task passes only if every repeat passes

    def verify(self) -> None:
        """Raise PromotionError if any frozen input differs from when it was frozen."""
        for task_id, path in self.tasks.items():
            if tree_digest(Path(path)) != self.digests[task_id]:
                raise PromotionError(f'frozen task {task_id!r} changed since it was frozen')
        if scorer_digest(self.scorer_paths) != self.digests['scorer']:
            raise PromotionError('the scoring command inputs changed since they were frozen')

    def fingerprint(self) -> str:
        """One hash naming this exact evaluation; scorecards are comparable only when it matches."""
        parts = [self.name, self.command, str(self.repeats), str(self.timeout)] + [f'{k}={self.digests[k]}' for k in sorted(self.digests)]
        return hashlib.sha256('\n'.join(parts).encode()).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def scorer_digest(paths: list[str]) -> str:
    return hashlib.sha256(''.join(tree_digest(Path(p)) for p in sorted(paths)).encode()).hexdigest()


def freeze(name: str, command: str, tasks: dict[str, Path], scorer_paths: list[Path] = (),
           timeout: float = 600, repeats: int = 1) -> Frozen:
    if not tasks:
        raise PromotionError('an evaluation needs at least one task')
    if not command.strip():
        raise PromotionError('an evaluation needs a scoring command')
    if repeats < 1:
        raise PromotionError('repeats must be at least 1')
    resolved = {k: str(Path(v).resolve()) for k, v in tasks.items()}
    for task_id, path in resolved.items():
        if not Path(path).is_dir():
            raise PromotionError(f'task {task_id!r}: {path} is not a directory')
    scorers = [str(Path(p).resolve()) for p in scorer_paths]
    digests = {k: tree_digest(Path(v)) for k, v in resolved.items()}
    digests['scorer'] = scorer_digest(scorers)
    return Frozen(name, command, resolved, digests, scorers, timeout, repeats)


@dataclass
class TaskScore:
    task: str
    passed: bool
    seconds: float
    runs: int
    detail: str = ''


@dataclass
class Scorecard:
    label: str
    identity: str                     # hash of the exact tree that was scored
    evaluation: str                   # frozen evaluation name
    evaluation_digest: str
    tasks: dict[str, TaskScore]
    peak_rss_mb: float
    scored_at: float = field(default_factory=time.time)

    @property
    def passed(self) -> set:
        return {t for t, s in self.tasks.items() if s.passed}

    @property
    def median_seconds(self) -> float:
        return statistics.median(s.seconds for s in self.tasks.values()) if self.tasks else 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(text: str) -> 'Scorecard':
        raw = json.loads(text)
        raw['tasks'] = {k: TaskScore(**v) for k, v in raw['tasks'].items()}
        return Scorecard(**raw)


def _run_measured(command: str, cwd: Path, env: dict, timeout: float) -> tuple[int | None, str, float]:
    """Run a shell command in its own process group. Returns (exit code or None on timeout, output tail, peak RSS MB).

    Peak memory comes from wait4 for this process tree only, so evaluations in one process do not
    inherit each other's maximum.
    """
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        deadline, timed_out = time.monotonic() + timeout, False
        while True:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if time.monotonic() > deadline:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                _, status, usage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.02)
        proc.returncode = os.waitstatus_to_exitcode(status)  # reaped here; keep Popen from waiting again
        out.seek(0)
        tail = out.read()[-500:].decode(errors='replace')
    scale = 1024 * 1024 if sys.platform == 'darwin' else 1024  # ru_maxrss: bytes on macOS, KiB elsewhere
    return (None if timed_out else proc.returncode), tail, usage.ru_maxrss / scale


def _run_task(frozen: Frozen, task_id: str, candidate_copy: Path) -> tuple[TaskScore, float]:
    started, peak = time.monotonic(), 0.0
    for run in range(1, frozen.repeats + 1):
        with tempfile.TemporaryDirectory(prefix='task-') as scratch:
            task_copy = Path(scratch) / 'task'
            shutil.copytree(frozen.tasks[task_id], task_copy)
            env = {**os.environ, 'NESSA_TASK_ID': task_id, 'NESSA_TASK_DIR': str(task_copy),
                   'NESSA_CANDIDATE_DIR': str(candidate_copy), 'PYTHONDONTWRITEBYTECODE': '1'}
            code, tail, rss = _run_measured(frozen.command, candidate_copy, env, frozen.timeout)
        peak = max(peak, rss)
        if code != 0:
            detail = f'timeout after {frozen.timeout}s' if code is None else (tail or f'exit {code}')
            return TaskScore(task_id, False, time.monotonic() - started, run, detail), peak
    return TaskScore(task_id, True, time.monotonic() - started, frozen.repeats), peak


def evaluate(frozen: Frozen, candidate_dir: Path, label: str) -> Scorecard:
    """Score a tree. The copy it runs from is fresh, so nothing a run writes survives or leaks."""
    frozen.verify()
    candidate_dir = Path(candidate_dir)
    if not candidate_dir.is_dir():
        raise PromotionError(f'{candidate_dir} is not a directory')
    identity = tree_digest(candidate_dir)
    with tempfile.TemporaryDirectory(prefix='candidate-') as scratch:
        copy = Path(scratch) / 'tree'
        shutil.copytree(candidate_dir, copy, ignore=IGNORED)
        results = {task_id: _run_task(frozen, task_id, copy) for task_id in frozen.tasks}
    frozen.verify()  # the run must not have altered the exam
    return Scorecard(label, identity, frozen.name, frozen.fingerprint(),
                     {task_id: score for task_id, (score, _) in results.items()},
                     round(max((rss for _, rss in results.values()), default=0.0), 1))


@dataclass
class Policy:
    """When a candidate may replace the baseline. Defaults are strict; loosen them deliberately."""
    max_regressions: int = 0          # tasks the baseline passed that the candidate fails
    min_improvements: int = 1         # tasks the candidate passes that the baseline failed
    max_slowdown: float = 1.5         # candidate median seconds / baseline median seconds
    max_memory_growth: float = 1.5    # candidate peak RSS / baseline peak RSS


@dataclass
class Decision:
    promote: bool
    reasons: list
    regressions: list
    improvements: list

    def to_dict(self) -> dict:
        return asdict(self)


def decide(baseline: Scorecard, candidate: Scorecard, policy: Policy = Policy()) -> Decision:
    reasons: list[str] = []
    if baseline.evaluation_digest != candidate.evaluation_digest:
        raise PromotionError('scorecards come from different evaluations and cannot be compared')
    if baseline.identity == candidate.identity:
        reasons.append('candidate is identical to the baseline')
    regressions = sorted(baseline.passed - candidate.passed)
    improvements = sorted(candidate.passed - baseline.passed)
    if len(regressions) > policy.max_regressions:
        reasons.append(f'{len(regressions)} regression(s): {", ".join(regressions)}')
    if len(improvements) < policy.min_improvements:
        reasons.append(f'{len(improvements)} improvement(s); policy requires {policy.min_improvements}')
    if baseline.median_seconds and candidate.median_seconds / baseline.median_seconds > policy.max_slowdown:
        reasons.append(f'median time {candidate.median_seconds:.2f}s vs {baseline.median_seconds:.2f}s '
                       f'exceeds {policy.max_slowdown}x')
    if baseline.peak_rss_mb and candidate.peak_rss_mb / baseline.peak_rss_mb > policy.max_memory_growth:
        reasons.append(f'peak memory {candidate.peak_rss_mb}MB vs {baseline.peak_rss_mb}MB '
                       f'exceeds {policy.max_memory_growth}x')
    return Decision(not reasons, reasons, regressions, improvements)


def promote(baseline: Scorecard, candidate: Scorecard, confirm: str, record_path: Path,
            policy: Policy = Policy()) -> Decision:
    """Record a promotion only when the policy passes and a person echoes the candidate's identity."""
    decision = decide(baseline, candidate, policy)
    if not decision.promote:
        raise PromotionError('not promoted: ' + '; '.join(decision.reasons))
    if confirm != candidate.identity[:12]:
        raise PromotionError(f'confirmation must be the candidate identity {candidate.identity[:12]}')
    record = dict(promoted_at=time.time(), baseline=asdict(baseline), candidate=asdict(candidate),
                  policy=asdict(policy), decision=decision.to_dict())
    Path(record_path).write_text(json.dumps(record, indent=2))
    return decision


def load_frozen(path: Path) -> Frozen:
    try:
        return Frozen(**json.loads(Path(path).read_text()))
    except (OSError, ValueError, TypeError) as exc:
        raise PromotionError(f'cannot read frozen evaluation {path}: {exc}') from exc


def load_scorecard(path: Path) -> Scorecard:
    try:
        return Scorecard.from_json(Path(path).read_text())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PromotionError(f'cannot read scorecard {path}: {exc}') from exc


def _cmd_freeze(a) -> int:
    tasks = {}
    for spec in a.task:
        task_id, sep, directory = spec.partition('=')
        if not sep or not task_id or not directory:
            raise PromotionError(f'--task must be ID=DIRECTORY, got {spec!r}')
        tasks[task_id] = Path(directory)
    frozen = freeze(a.name, a.command, tasks, [Path(p) for p in a.scorer], a.timeout, a.repeats)
    Path(a.out).write_text(frozen.to_json())
    print(f'froze {len(tasks)} task(s); evaluation {frozen.fingerprint()[:12]} -> {a.out}')
    return 0


def _cmd_evaluate(a) -> int:
    card = evaluate(load_frozen(a.frozen), Path(a.candidate), a.label)
    Path(a.out).write_text(card.to_json())
    print(f'{a.label}: {len(card.passed)}/{len(card.tasks)} passed, median {card.median_seconds:.2f}s, '
          f'peak {card.peak_rss_mb}MB, identity {card.identity[:12]} -> {a.out}')
    return 0


def _cmd_decide(a) -> int:
    policy = Policy(a.max_regressions, a.min_improvements, a.max_slowdown, a.max_memory_growth)
    baseline, candidate = load_scorecard(a.baseline), load_scorecard(a.candidate)
    if not a.confirm:
        decision = decide(baseline, candidate, policy)
        print(json.dumps(decision.to_dict(), indent=2))
        if decision.promote:
            print(f'Policy passes. To record the promotion, rerun with --confirm {candidate.identity[:12]}')
        return 0 if decision.promote else 1
    promote(baseline, candidate, a.confirm, Path(a.record), policy)
    print(f'promotion recorded -> {a.record}')
    return 0


def register(sub) -> None:
    """Attach `promote freeze|evaluate|decide` to the main CLI."""
    top = sub.add_parser('promote', help='score a candidate harness outside its workspace and gate promotion')
    steps = top.add_subparsers(dest='step', required=True)
    f = steps.add_parser('freeze', help='hash tasks and scorer into an immutable evaluation')
    f.add_argument('--name', required=True)
    f.add_argument('--command', required=True, help='scoring command run once per task; exit 0 = pass')
    f.add_argument('--task', action='append', required=True, metavar='ID=DIRECTORY')
    f.add_argument('--scorer', action='append', default=[], help='file or directory the command depends on')
    f.add_argument('--timeout', type=float, default=600)
    f.add_argument('--repeats', type=int, default=1)
    f.add_argument('--out', default='frozen.json')
    f.set_defaults(fn=_cmd_freeze)
    e = steps.add_parser('evaluate', help='score one tree against a frozen evaluation')
    e.add_argument('frozen')
    e.add_argument('candidate', help='directory of the harness version to score')
    e.add_argument('--label', required=True, help='e.g. baseline or candidate')
    e.add_argument('--out', required=True)
    e.set_defaults(fn=_cmd_evaluate)
    d = steps.add_parser('decide', help='apply the policy; add --confirm to record the promotion')
    d.add_argument('baseline')
    d.add_argument('candidate')
    d.add_argument('--confirm', default='', help="the candidate's 12-character identity")
    d.add_argument('--record', default='promotion.json')
    defaults = Policy()
    d.add_argument('--max-regressions', type=int, default=defaults.max_regressions)
    d.add_argument('--min-improvements', type=int, default=defaults.min_improvements)
    d.add_argument('--max-slowdown', type=float, default=defaults.max_slowdown)
    d.add_argument('--max-memory-growth', type=float, default=defaults.max_memory_growth)
    d.set_defaults(fn=_cmd_decide)
