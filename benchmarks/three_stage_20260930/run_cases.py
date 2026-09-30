"""Reproduce the five synthetic repairs with a chosen NESSA checkout/local model.

This runner exercises trusted synthetic code. It is not an OS sandbox.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from grading import independent_grade


def manifest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--code', type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument('--out', type=Path, default=Path.home() / 'nessa-three-stage-benchmarks' /
                   time.strftime('%Y%m%dT%H%M%SZ'))
    p.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    p.add_argument('--model', default='qwen2.5-coder:1.5b')
    p.add_argument('--validate-fixtures', action='store_true', help='prove original tests fail without calling a model')
    a = p.parse_args()
    if a.out.exists() and any(a.out.iterdir()):
        p.error('--out must be absent or empty; old evidence will not be overwritten')
    a.out.mkdir(parents=True, exist_ok=True)
    a.out = a.out.resolve()
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())
    summary = []
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    for name, case in cases.items():
        case_root = a.out / name
        fixture = case_root / 'fixture'
        for path, content in case['files'].items():
            dest = fixture / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        original = manifest(fixture)
        check_argv = [sys.executable, '-m', 'unittest', 'discover', '-s', 'checks', '-p', '*.py', '-q']
        baseline = subprocess.run(check_argv, cwd=fixture, env=env, capture_output=True, text=True)
        (case_root / 'fixture-baseline.log').write_text(baseline.stdout + baseline.stderr)
        if baseline.returncode == 0:
            raise RuntimeError(f'{name}: deliberately broken fixture unexpectedly passed')
        if a.validate_fixtures:
            summary.append({'case': name, 'original_tests_failed': True})
            continue
        work = case_root / 'work'
        args = [sys.executable, '-m', 'agentharness.three_stage', str(fixture), case['task'],
                '--work', str(work), '--base-url', a.base_url, '--model', a.model,
                '--text-tools', '--auto-approve', '--max-tokens', '1024', '--timeout', '120',
                '--time-budget', '600', '--check',
                f'tests="{sys.executable}" -m unittest discover -s checks -p "*.py" -q']
        (case_root / 'command.json').write_text(json.dumps(args, indent=2))
        run = subprocess.run(args, cwd=a.code.resolve(), env=env, capture_output=True, text=True)
        (case_root / 'console.log').write_text(run.stdout + run.stderr)
        result_path = work / 'evidence/result.json'
        result = json.loads(result_path.read_text()) if result_path.is_file() else {'status': 'setup_error'}
        actual = manifest(work / 'repo') if (work / 'repo').is_dir() else {}
        try:
            grade = independent_grade(name, work / 'repo') if actual else None
        except Exception as exc:
            grade = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
        row = {'case': name, 'status': result['status'], 'cli_returncode': run.returncode,
               'original_unchanged': manifest(fixture) == original,
               'tests_unchanged': all(actual.get(k) == v for k, v in original.items() if k.startswith('checks/')),
               'independent_grade': grade}
        patch = work / 'evidence/patch.diff'
        if patch.is_file() and patch.read_text().strip():
            replay = case_root / 'patch-replay'
            shutil.copytree(fixture, replay)
            applied = subprocess.run(['git', 'apply', '--check', str(patch)], cwd=replay, capture_output=True, text=True)
            if applied.returncode == 0:
                applied = subprocess.run(['git', 'apply', str(patch)], cwd=replay, capture_output=True, text=True)
            row['patch_replay_returncode'] = applied.returncode
            if applied.returncode == 0:
                checked = subprocess.run(check_argv, cwd=replay, env=env, capture_output=True, text=True)
                row['patch_replay_tests_returncode'] = checked.returncode
        summary.append(row)
        print(json.dumps(row), flush=True)
        (a.out / 'summary.json').write_text(json.dumps(summary, indent=2))
    (a.out / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(f'Evidence: {a.out}')
    return 0 if a.validate_fixtures or all(r['status'] == 'verified' and r['tests_unchanged'] and
        r['original_unchanged'] and r['independent_grade']['status'] == 'passed' and
        r.get('patch_replay_returncode') == r.get('patch_replay_tests_returncode') == 0 for r in summary) else 1


if __name__ == '__main__':
    raise SystemExit(main())
