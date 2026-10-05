"""Native LFM routing and isolated installation/readiness behavior."""
import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentharness.llm import ChatClient
from agentharness.profiles import apply_profile
from agentharness.tests.test_http_cli import FakeServer
from agentharness.__main__ import main

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install_laptop.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class LFM(unittest.TestCase):
    def test_lfm_sends_history_without_assistant_prefill(self):
        messages = [{'role': 'user', 'content': 'My name is alabs.'},
                    {'role': 'assistant', 'content': 'Hello, alabs!'},
                    {'role': 'user', 'content': 'What is my name?'}]
        for model, effort, prefill in [('nessa-lfm:latest', 'none', False),
                                       ('lfm2.5:8b-a1b-q4_K_M', 'none', False),
                                       ('nessa-lfm:latest', None, False),
                                       ('other-model', 'none', False)]:
            with self.subTest(model=model, effort=effort):
                client = ChatClient('http://localhost:11435/v1', model, reasoning_effort=effort)
                with patch.object(client, '_request', return_value={
                        'choices': [{'message': {'content': 'alabs'}}]}) as request:
                    self.assertEqual(client.chat(messages).content, 'alabs')
                sent = request.call_args.args[1]['messages']
                self.assertEqual(sent[:3], messages)
                self.assertEqual(len(messages), 3)
                self.assertEqual(len(sent), 4 if prefill else 3)
                if prefill:
                    self.assertEqual(sent[-1], {'role': 'assistant', 'content': '<think></think>'})

    def test_inline_reasoning_keeps_native_calls_outside_renderer_cleanup(self):
        client = ChatClient('http://127.0.0.1:11435/v1', 'nessa-lfm:latest')
        call = {'id': 'read', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"calc.py"}'}}
        response = {'choices': [{'message': {
            'content': '<think>Inspect the source first.</think>\n',
            'tool_calls': [call]}}]}
        with patch.object(client, '_request', return_value=response):
            reply = client.chat([{'role': 'user', 'content': 'Fix add'}])
        self.assertTrue(reply.native)
        self.assertEqual(reply.content, '')
        self.assertEqual(reply.reasoning, 'Inspect the source first.')
        self.assertEqual(reply.raw_tool_calls, [call])
        self.assertEqual(reply.tool_calls[0].arguments, {'path': 'calc.py'})

    def test_profile_uses_native_tools_without_overriding_explicit_model(self):
        args = apply_profile(argparse.Namespace(profile='lfm-i3-12gb', model='custom'))
        self.assertEqual(args.model, 'custom')
        self.assertFalse(args.text_tools)  # LFM JSON-text calls omitted tool names in live runs
        self.assertEqual(args.reasoning_effort, 'none')
        self.assertEqual(args.temperature, 0.2)

    def test_lfm_http_loop_passes_native_tool_results(self):
        server = FakeServer('native')
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.mkdir()
            (source / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
            with redirect_stdout(io.StringIO()):
                code = main(['run', str(source), 'Fix add', '--profile', 'lfm-i3-12gb',
                             '--model', 'fake-model', '--base-url', server.url, '--auto-approve',
                             '--work', str(Path(tmp) / 'work')])
            self.assertEqual(code, 0)
            offered = {t['function']['name'] for t in server.requests[0]['tools']}
            self.assertIn('read_file', offered)
            self.assertIn('propose_plan', offered)
            self.assertFalse(offered & {'studio_control', 'weather', 'local_list'})  # assistant-only tools
            for request in server.requests:
                self.assertEqual(request['reasoning_effort'], 'none')
                self.assertEqual(request['max_tokens'], 3072)
                self.assertEqual(request['temperature'], 0.2)
                payload = len(json.dumps(request['messages'], ensure_ascii=False)) + len(json.dumps(request.get('tools', [])))
                self.assertLessEqual(payload, 18000)
            self.assertTrue(any(m['role'] == 'tool' for m in server.requests[-1]['messages']))

    def test_default_client_does_not_force_backend_reasoning_setting(self):
        client = ChatClient('http://localhost:11434/v1', 'm')
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': 'ok'}}]}) as request:
            client.chat([])
        self.assertNotIn('reasoning_effort', request.call_args.args[1])

    def test_unit_limits_and_path_escaping(self):
        unit = installer.unit_text(Path('/tmp/user %/bin/ollama'), Path('/tmp/user %/models'))
        self.assertIn('user %%', unit)
        self.assertIn('OLLAMA_HOST=127.0.0.1:11435', unit)
        self.assertIn('OLLAMA_NUM_PARALLEL=1', unit)
        self.assertIn('OLLAMA_MAX_LOADED_MODELS=1', unit)

    def test_launcher_shell_syntax_with_spaces_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'nessa'
            script.write_text(installer.launcher_text(Path("/tmp/my app's directory"), '/usr/bin/python3'))
            subprocess.run(['bash', '-n', str(script)], check=True)

    def _install(self, skip=False, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / '.local/share/nessa'
            binary = root / 'runtime/bin/ollama'
            binary.parent.mkdir(parents=True)
            binary.write_text('fake binary')
            commands = []
            def run(argv, **kw):
                argv = list(map(str, argv))
                commands.append(argv)
                if any('smoke_local.py' in x for x in argv):
                    if fail:
                        raise subprocess.CalledProcessError(1, argv)
                    out = Path(argv[argv.index('--out') + 1])
                    out.mkdir(parents=True)
                    (out / 'acceptance.json').write_text('{"passed": true}')
            with patch.object(Path, 'home', return_value=home), \
                 patch.object(installer.os, 'geteuid', return_value=1000), \
                 patch.object(installer.platform, 'system', return_value='Linux'), \
                 patch.object(installer.platform, 'machine', return_value='x86_64'), \
                 patch.object(installer.shutil, 'which', return_value='/bin/fake'), \
                 patch.object(installer.shutil, 'disk_usage', return_value=argparse.Namespace(free=30*1024**3)), \
                 patch.object(installer, 'run', side_effect=run), \
                 patch.object(installer, 'get_json', return_value={'version': 'test'}), \
                 patch.object(installer.socket.socket, 'connect_ex', return_value=1), \
                 redirect_stdout(io.StringIO()):
                code = installer.install(skip_acceptance=skip)
            report = json.loads((root / 'installation.json').read_text())
            self.assertTrue((home / '.local/bin/nessa').is_file())
            self.assertTrue((root / 'app/agentharness/llm.py').is_file())
            return code, report, commands

    def test_install_ready_only_after_acceptance(self):
        code, report, commands = self._install()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ready')
        self.assertTrue(report['acceptance_passed'])
        self.assertTrue(any('pull' in command and installer.MODEL in command for command in commands))

    def test_failed_acceptance_is_not_ready(self):
        code, report, _ = self._install(fail=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['acceptance_passed'])

    def test_skipped_acceptance_is_unverified(self):
        code, report, commands = self._install(skip=True)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'installed_unverified')
        self.assertFalse(any(any('smoke_local.py' in x for x in command) for command in commands))


if __name__ == '__main__':
    unittest.main()


class WarmChat(unittest.TestCase):
    @staticmethod
    def module():
        spec = importlib.util.spec_from_file_location('warm_chat', ROOT / 'scripts/warm_chat.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # must not touch the network on import
        return module

    def serve(self, replies):
        import http.server
        import threading
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(handler):
                seen.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
                body = json.dumps(replies.pop(0)).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}', seen

    def test_import_has_no_side_effects_and_warm_requests_a_resident_model(self):
        warm = self.module().warm
        url, seen = self.serve([{'done': True}])
        warm(url, 'tiny:latest')
        self.assertEqual(seen, [{'model': 'tiny:latest', 'keep_alive': -1, 'stream': False}])

    def test_incomplete_load_is_an_error_and_unreachable_server_is_retried_then_raised(self):
        warm = self.module().warm
        url, _ = self.serve([{'done': False}])
        with self.assertRaises(RuntimeError):
            warm(url, 'tiny:latest')
        with self.assertRaises(OSError):
            warm('http://127.0.0.1:9', 'tiny:latest', attempts=2, retry_seconds=0)
