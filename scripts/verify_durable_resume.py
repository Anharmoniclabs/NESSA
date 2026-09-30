#!/usr/bin/env python3
"""A real process SIGINT/resume integration with fake loopback HTTP, no inference.

Exercises both transports, checks step accounting and the discarded unknown
response, and proves a terminal resume makes zero additional model requests.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentharness.durable import StateStore
from agentharness.tests.test_agent import BUGGY, make_project
from agentharness.tests.test_code_proposals import GOOD, PLAN


def exercise(transport):
    received = threading.Event()
    release = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/api/show':
                data = {'thinking': {'values': [False]}}
            else:
                calls.append(body)
                number = len(calls)
                if number == 1:
                    content = json.dumps({'name': 'propose_plan', 'arguments': PLAN})
                elif number in (2, 3):
                    content = GOOD
                    if number == 2:
                        received.set()
                        release.wait(15)
                else:
                    content = 'Controller facts recorded.'
                if transport == 'ollama-native':
                    data = {'done': True, 'done_reason': 'stop', 'message': {
                            'role': 'assistant', 'content': content}, 'eval_count': 10}
                else:
                    data = {'choices': [{'message': {'role': 'assistant', 'content': content},
                                         'finish_reason': 'stop'}]}
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='nessa-durable-integration-') as temp:
            root = Path(temp)
            project = make_project(root)
            work = root / 'work'
            base = f'http://127.0.0.1:{server.server_port}' + ('/v1' if transport == 'openai' else '')
            command = [sys.executable, '-m', 'agentharness.durable', 'start', str(project), 'Fix addition',
                '--work', str(work), '--auto-approve', '--text-tools', '--base-url', base,
                '--model', 'fake-resume-fixture', '--transport', transport, '--timeout', '5', '--time-budget', '120']
            if transport == 'ollama-native':
                command += ['--ollama-think', 'false', '--ollama-seed', '17', '--ollama-num-ctx', '2048']
            proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                assert received.wait(15), 'Second (source) model request was not reached'
                proc.send_signal(signal.SIGINT)
                output, _ = proc.communicate(timeout=15)
                assert proc.returncode == 130, (proc.returncode, output)
            finally:
                release.set()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            saved = StateStore(work).load()
            assert saved['next_step'] == 'model_pending', saved['next_step']
            assert saved['steps'] == 1 and not saved['journal']
            assert (work / 'repo/calc.py').read_text() == BUGGY
            resumed = subprocess.run([sys.executable, '-m', 'agentharness.durable', 'resume', str(work), '--steps', '1'],
                                     cwd=ROOT, text=True, capture_output=True, timeout=30)
            assert resumed.returncode == 0, resumed.stdout + resumed.stderr
            saved = StateStore(work).load()
            assert saved['result']['status'] == 'verified'
            assert saved['steps'] == 2 and len(saved['journal']) == 1
            assert any(r['outcome'] == 'discarded_unknown_response' for r in saved['receipts'])
            assert (project / 'calc.py').read_text() == BUGGY
            assert (work / 'repo/calc.py').read_text() == GOOD
            total = len(calls)
            before = {str(p): p.read_bytes() for p in work.rglob('*') if p.is_file()}
            noop = subprocess.run([sys.executable, '-m', 'agentharness.durable', 'resume', str(work), '--steps', '1'],
                                 cwd=ROOT, text=True, capture_output=True, timeout=10)
            assert noop.returncode == 0 and len(calls) == total
            assert before == {str(p): p.read_bytes() for p in work.rglob('*') if p.is_file()}
            if transport == 'ollama-native':
                assert all(c['think'] is False and c['options']['seed'] == 17 and c['options']['num_ctx'] == 2048 for c in calls)
            return {'transport': transport, 'interrupt_exit': 130, 'status': 'verified',
                    'steps': saved['steps'], 'edits': len(saved['journal']),
                    'model_requests': total, 'terminal_resume': 'byte-for-byte no-op',
                    'original_source': 'unchanged'}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    if os.name != 'posix':
        raise SystemExit('This POSIX SIGINT integration requires POSIX.')
    print(json.dumps([exercise('openai'), exercise('ollama-native')], indent=2))
