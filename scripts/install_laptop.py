"""Install a private CPU Ollama runtime and NESSA for an x86_64 Linux desktop user."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
MODEL = 'lfm2.5:8b-a1b-q4_K_M'
ALIAS = 'nessa-lfm:latest'
HOST = '127.0.0.1:11435'
BASE_URL = f'http://{HOST}/v1'
PROFILE = 'lfm-i3-12gb'


def run(argv, **kwargs):
    print('+', shlex.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def get_json(route):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://{HOST}{route}', timeout=3) as response:
        return json.load(response)


def unit_text(ollama: Path, models: Path) -> str:
    def quoted(value):
        # systemd specifiers must be escaped even within quoted arguments.
        return '"' + str(value).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'
    return f'''[Unit]
Description=NESSA local CPU model server
After=network.target

[Service]
ExecStart={quoted(ollama)} serve
Environment="OLLAMA_HOST={HOST}"
Environment={quoted('OLLAMA_MODELS=' + str(models))}
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_CONTEXT_LENGTH=8192"
Environment="OLLAMA_KEEP_ALIVE=5m"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
'''


def launcher_text(app: Path, python: str) -> str:
    return f'''#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH={shlex.quote(str(app))}${{PYTHONPATH:+:$PYTHONPATH}}
if [[ $# == 0 ]]; then set -- --help; fi
case "$1" in
  chat|run|resume|doctor|batch)
    systemctl --user start nessa-ollama.service
    exec {shlex.quote(python)} -m agentharness "$@" --profile {PROFILE} --base-url {BASE_URL}
    ;;
  smoke)
    shift
    systemctl --user start nessa-ollama.service
    exec {shlex.quote(python)} {shlex.quote(str(app / 'scripts/smoke_local.py'))} --profile {PROFILE} --base-url {BASE_URL} "$@"
    ;;
  *) exec {shlex.quote(python)} -m agentharness "$@" ;;
esac
'''


def install(update_runtime=False, skip_acceptance=False):
    if os.geteuid() == 0 or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Run as a normal user on x86_64 Linux')
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10 or newer is required')
    for name in ('curl', 'tar', 'zstd', 'systemctl', 'git'):
        if not shutil.which(name):
            raise RuntimeError(f'Missing {name}; run bash scripts/install_laptop.sh')
    run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL)
    root = Path.home() / '.local/share/nessa'
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / 'installation.json'
    report = dict(status='installing', model=MODEL, alias=ALIAS, profile=PROFILE,
                  base_url=BASE_URL, started_at=time.time(), acceptance_passed=False)
    report_path.write_text(json.dumps(report, indent=2))
    try:
        # Reserve disk headroom for the model, archive, runtime and evidence.
        if shutil.disk_usage(root).free < 16 * 1024**3:
            raise RuntimeError('At least 16 GiB free disk space is required for installation')
        unit = Path.home() / '.config/systemd/user/nessa-ollama.service'
        if not unit.exists():
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', 11435)) == 0:
                    raise RuntimeError('Port 11435 is already occupied; stop that server before installing NESSA')
        runtime = root / 'runtime'
        ollama = runtime / 'bin/ollama'
        if not ollama.exists() or update_runtime:
            with tempfile.TemporaryDirectory(prefix='runtime-', dir=root) as temp:
                stage = Path(temp)
                archive = stage / 'ollama.tar.zst'
                run(['curl', '--fail', '--location', '--retry', '3', '--output', archive,
                     'https://ollama.com/download/ollama-linux-amd64.tar.zst'])
                unpacked = stage / 'unpacked'
                unpacked.mkdir()
                run(['tar', '--zstd', '-xf', archive, '-C', unpacked])
                if not (unpacked / 'bin/ollama').is_file():
                    raise RuntimeError('Downloaded runtime is missing bin/ollama')
                if unit.exists():
                    run(['systemctl', '--user', 'stop', 'nessa-ollama.service'])
                if runtime.exists():
                    runtime.rename(root / f'runtime-backup-{time.time_ns()}')
                unpacked.rename(runtime)
        app = root / 'app'
        with tempfile.TemporaryDirectory(prefix='app-', dir=root) as temp:
            stage = Path(temp) / 'app'
            stage.mkdir()
            for name in ('agentharness', 'scripts', 'configs', 'docs'):
                shutil.copytree(SOURCE / name, stage / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(SOURCE / 'README.md', stage / 'README.md')
            if app.exists():
                app.rename(root / f'app-backup-{time.time_ns()}')
            stage.rename(app)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(unit_text(ollama, root / 'models'))
        run(['systemctl', '--user', 'daemon-reload'])
        run(['systemctl', '--user', 'enable', '--now', 'nessa-ollama.service'])
        for attempt in range(30):
            try:
                get_json('/api/version')
                break
            except (OSError, ValueError):
                time.sleep(1)
        else:
            raise RuntimeError('Model server did not start; inspect journalctl --user -u nessa-ollama.service')
        env = {**os.environ, 'OLLAMA_HOST': HOST}
        run([ollama, 'pull', MODEL], env=env)
        run([ollama, 'create', ALIAS, '-f', app / 'configs/Modelfile.lfm-i3'], env=env)
        report['runtime'] = get_json('/api/version')
        report['models'] = get_json('/api/tags')
        if Path('/proc/meminfo').exists():
            report['memory_before_inference'] = Path('/proc/meminfo').read_text()
        report['hardware'] = dict(cpu=platform.processor(), machine=platform.machine(),
                                  logical_cpus=os.cpu_count())
        bin_dir = Path.home() / '.local/bin'
        bin_dir.mkdir(parents=True, exist_ok=True)
        launcher = bin_dir / 'nessa'
        if launcher.exists() and 'nessa-ollama.service' not in launcher.read_text(errors='replace'):
            raise RuntimeError(f'{launcher} already exists and is not a NESSA launcher; refusing overwrite')
        launcher.write_text(launcher_text(app, sys.executable))
        launcher.chmod(0o755)
        environment = {**os.environ, 'PYTHONPATH': str(app)}
        run([sys.executable, '-m', 'agentharness', 'doctor', '--profile', PROFILE,
             '--base-url', BASE_URL], cwd=app, env=environment)
        # Real inference is the final gate. Never mark a skipped or failed test ready.
        if not skip_acceptance:
            acceptance = root / 'acceptance' / str(time.time_ns())
            report['acceptance_dir'] = str(acceptance)
            run([sys.executable, app / 'scripts/smoke_local.py', '--profile', PROFILE,
                 '--base-url', BASE_URL, '--out', acceptance], cwd=app, env=environment)
            report['acceptance_passed'] = json.loads((acceptance / 'acceptance.json').read_text())['passed'] is True
            if not report['acceptance_passed']:
                raise RuntimeError('Real-model acceptance did not pass')
        report['status'] = 'ready' if report['acceptance_passed'] else 'installed_unverified'
        print(f"\n{report['status'].upper()}: {launcher} chat /path/to/project")
        print(f'Report: {report_path}')
        return 0
    except Exception as exc:
        report.update(status='failed', error=str(exc))
        print(f'Installation not ready: {exc}', file=sys.stderr)
        return 1
    finally:
        report['finished_at'] = time.time()
        report_path.write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update-runtime', action='store_true')
    parser.add_argument('--skip-acceptance', action='store_true', help='install only; report remains unverified')
    args = parser.parse_args()
    raise SystemExit(install(args.update_runtime, args.skip_acceptance))
