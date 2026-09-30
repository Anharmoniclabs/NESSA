"""Native Ollama transport contract tests; all HTTP replies are synthetic."""
import io
import json
import os
import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentharness.llm import ModelError
from agentharness.ollama import OllamaClient, OllamaDeadlineExceeded
from agentharness.three_stage import main
from agentharness.tests.test_agent import BUGGY, make_project
from agentharness.tests.test_three_stage import EDIT, PLAN

PRIVATE = "PRIVATE_THINKING_SENTINEL: never retain this text"
GOOD = BUGGY.replace("a - b", "a + b")


def response(content="done", *, calls=None, reason="stop", **extra):
    return {"done": True, "done_reason": reason,
        "message": {"role": "assistant", "content": content, "thinking": PRIVATE,
                    "tool_calls": calls or []},
        "prompt_eval_count": 100, "prompt_eval_cached_count": 20, "eval_count": 150,
        "total_duration": 2000000000, "load_duration": 1000000,
        "prompt_eval_duration": 500000000, "eval_duration": 1499000000, **extra}


def native(name, args):
    return {"function": {"name": name, "arguments": args}}


class NativeServer:
    def __init__(self, replies=(), show=None, status=200, delay=0):
        self.requests, self.replies = [], list(replies)
        self.show = {"thinking": {"values": [True, False], "default": False}} if show is None else show
        outer = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, dict(self.headers)))
                if self.path == "/api/show":
                    data, code = outer.show, 200
                else:
                    if delay:
                        time.sleep(delay)
                    data = outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]
                    code = status
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NativeTransportTests(unittest.TestCase):
    def server(self, *a, **kw):
        server = NativeServer(*a, **kw)
        self.addCleanup(server.close)
        return server

    def test_request_uses_native_template_explicit_mode_and_shared_budget(self):
        server = self.server([response()])
        client = OllamaClient(server.url, "model", think=True, max_tokens=1024)
        reply = client.chat([{"role": "user", "content": "hi", "thinking": PRIVATE}])
        self.assertEqual([r[0] for r in server.requests], ["/api/show", "/api/chat"])
        body = server.requests[-1][1]
        self.assertEqual(body["think"], True)
        self.assertFalse(body["stream"])
        self.assertEqual(body["options"], {"num_ctx": 8192, "num_thread": 2, "seed": 42,
                                           "temperature": 0.0, "num_predict": 1024})
        self.assertNotIn("template", body)
        self.assertNotIn("raw", body)
        self.assertNotIn("Authorization", server.requests[-1][2])
        self.assertNotIn(PRIVATE, json.dumps(body))
        self.assertEqual(reply.prompt_tokens, 100)
        self.assertEqual(reply.completion_tokens, 150)
        self.assertEqual(reply.cached_prompt_tokens, 20)
        self.assertEqual(reply.transport_metadata["thinking_chars"], len(PRIVATE))
        self.assertIsNone(reply.reasoning)
        self.assertNotIn(PRIVATE, json.dumps(asdict(reply)))

    def test_exact_typed_mode_metadata_discovery(self):
        cases = [(True, [False], False), (True, [1], False), ("high", ["low", "high"], True),
                 ("HIGH", ["low", "high"], False), (False, [False], True)]
        for think, values, expected in cases:
            with self.subTest(think=think, values=values):
                server = self.server([response()], show={"thinking": {"values": values}})
                client = OllamaClient(server.url, "model", think=think)
                if expected:
                    client.chat([{"role": "user", "content": "hi"}])
                    self.assertEqual(server.requests[-1][1]["think"], think)
                else:
                    with self.assertRaises(ModelError):
                        client.chat([{"role": "user", "content": "hi"}])
                    self.assertEqual(len(server.requests), 1)

    def test_missing_thinking_metadata_fails_without_fallback(self):
        server = self.server([response()], show={"capabilities": ["completion"]})
        with self.assertRaisesRegex(ModelError, "metadata"):
            OllamaClient(server.url, "model", think=True).discover()
        self.assertEqual(len(server.requests), 1)

    def test_legacy_thinking_capability_allows_only_explicit_true(self):
        for think in (True, False, "high"):
            server = self.server([response()], show={"capabilities": ["completion", "thinking"]})
            client = OllamaClient(server.url, "m", think=think)
            if think is True:
                reply = client.chat([{"role": "user", "content": "hi"}])
                self.assertIn("legacy", reply.transport_metadata["thinking_metadata_source"])
            else:
                with self.assertRaises(ModelError):
                    client.discover()
                self.assertEqual(len(server.requests), 1)

    def test_malformed_present_metadata_is_not_legacy_support(self):
        for show in ({"thinking": None, "capabilities": ["thinking"]},
                     {"thinking": {}, "capabilities": ["thinking"]},
                     {"capabilities": "thinking"}, {"capabilities": [1, "thinking"]}):
            server = self.server([response()], show=show)
            with self.assertRaises(ModelError):
                OllamaClient(server.url, "m", think=True).discover()
            self.assertEqual(len(server.requests), 1)

    def test_remote_backed_model_refused_before_chat(self):
        for field in ("remote_host", "remote_model"):
            server = self.server([response()], show={"thinking": {"values": [True]}, field: "remote"})
            with self.assertRaisesRegex(ModelError, "remote-backed"):
                OllamaClient(server.url, "m", think=True).chat([{"role": "user", "content": "source"}])
            self.assertEqual(len(server.requests), 1)

    def test_over_budget_and_remote_marked_response_cannot_propose(self):
        for data in (response(calls=[native("propose_plan", PLAN)], eval_count=999),
                     response(remote_host="remote")):
            server = self.server([data])
            with self.assertRaises(ModelError):
                OllamaClient(server.url, "m", think=True, max_tokens=200).chat([{"role": "user", "content": "hi"}])

    def test_loopback_origin_and_parameter_validation(self):
        for url in ("http://example.com", "https://localhost", "http://localhost/v1",
                    "http://u:p@localhost", "http://localhost/?x=y", "http://localhost/#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaClient(url, "m", think=True)
        for kw in ({"think": 1}, {"think": None}, {"max_tokens": 1025}, {"max_tokens": -1},
                   {"timeout": float("inf")}, {"num_thread": 0}, {"num_ctx": 0}, {"seed": -1}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                OllamaClient("http://localhost:11434", "m", **{"think": True, **kw})

    def test_native_and_text_calls_map_to_existing_contract(self):
        for data, expected_native in ((response(calls=[native("propose_plan", PLAN)]), True),
            (response(json.dumps({"name": "propose_plan", "arguments": PLAN})), False)):
            with self.subTest(native=expected_native):
                server = self.server([data])
                reply = OllamaClient(server.url, "m", think=True).chat(
                    [{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "propose_plan"}}])
                self.assertEqual(reply.native, expected_native)
                self.assertEqual(reply.tool_calls[0].arguments, PLAN)
                self.assertIn("tools", server.requests[-1][1])
                self.assertNotIn(PRIVATE, json.dumps(asdict(reply)))

    def test_length_drops_even_complete_looking_native_or_text_actions(self):
        for data in (response(calls=[native("propose_edit", EDIT)], reason="length"),
                     response(json.dumps({"name": "propose_plan", "arguments": PLAN}), reason="length")):
            server = self.server([data])
            reply = OllamaClient(server.url, "m", think=True).chat([{"role": "user", "content": "hi"}])
            self.assertEqual(reply.finish_reason, "length")
            self.assertEqual(reply.content, "")
            self.assertFalse(reply.tool_calls)
            self.assertFalse(reply.raw_tool_calls)

    def test_malformed_or_unsupported_completion_is_honest_and_redacted(self):
        variants = [response(done=False), response(reason="cancelled"), response(reason=None),
            response(""), response("</think>" + PRIVATE), response("<think>" + PRIVATE + "</think>answer"),
            response(message={"role": "assistant", "content": "ok", "thinking": {"x": PRIVATE}}),
            response(eval_count=-1), response(calls=[native("x", PRIVATE)]),
            {"error": PRIVATE}, [PRIVATE]]
        for data in variants:
            with self.subTest(data_type=type(data).__name__):
                server = self.server([data])
                with self.assertRaises(ModelError) as error:
                    OllamaClient(server.url, "m", think=True).chat([{"role": "user", "content": "hi"}])
                self.assertNotIn(PRIVATE, str(error.exception))

    def test_http_failure_has_no_body_echo_or_retry(self):
        server = self.server([{"error": PRIVATE}], status=400)
        with self.assertRaisesRegex(ModelError, "HTTP 400") as error:
            OllamaClient(server.url, "m", think=True).chat([{"role": "user", "content": "hi"}])
        self.assertNotIn(PRIVATE, str(error.exception))
        self.assertEqual(len(server.requests), 2)

    def test_timeout_rejects_without_retry(self):
        server = self.server([response()], delay=.1)
        with self.assertRaisesRegex(OllamaDeadlineExceeded, "deadline exceeded"):
            OllamaClient(server.url, "m", think=True, timeout=.025).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(server.requests), 2)


class NativeSocketTests(unittest.TestCase):
    def raw_server(self, payload, interval=0):
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        def run():
            try:
                sock, _ = listener.accept()
                with sock:
                    sock.recv(4096)
                    for part in payload:
                        sock.sendall(part)
                        if interval:
                            time.sleep(interval)
            except OSError:
                pass
        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    def test_slow_body_and_header_trickle_have_elapsed_deadline(self):
        sequences = [
            [b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n"] + [b"x"] * 30,
            [b"HTTP/1.1 200 OK\r\n"] + [b"X-Keep: alive\r\n"] * 30,
        ]
        for sequence in sequences:
            url = self.raw_server(sequence, interval=.025)
            client = OllamaClient(url, "m", think=True, timeout=.08)
            start = time.monotonic()
            with self.assertRaisesRegex(OllamaDeadlineExceeded, "deadline exceeded"):
                client.discover()
            self.assertLess(time.monotonic() - start, .3)

    def test_malformed_status_line_does_not_echo_server_text(self):
        url = self.raw_server([(PRIVATE + "\r\n").encode()])
        with self.assertRaises(ModelError) as error:
            OllamaClient(url, "m", think=True).discover()
        self.assertNotIn(PRIVATE, str(error.exception))
        self.assertIn("BadStatusLine", str(error.exception))

    def test_early_disconnect_is_not_mislabeled_as_a_deadline(self):
        url = self.raw_server([b""])
        with self.assertRaises(ModelError) as error:
            OllamaClient(url, "m", think=True).discover()
        self.assertNotIsInstance(error.exception, OllamaDeadlineExceeded)
        self.assertIn("RemoteDisconnected", str(error.exception))

    def test_redirect_is_not_followed(self):
        url = self.raw_server([b"HTTP/1.1 302 Found\r\nLocation: http://example.com/\r\nContent-Length: 0\r\n\r\n"])
        with self.assertRaisesRegex(ModelError, "HTTP 302"):
            OllamaClient(url, "m", think=True).discover()


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = make_project(self.tmp)

    def run_cli(self, replies, *, solve_mode="code-only", approve=True, extra=()):
        server = NativeServer(replies)
        self.addCleanup(server.close)
        work = self.tmp / "work"
        args = [str(self.project), "Fix addition", "--work", str(work), "--transport", "ollama-native",
                "--base-url", server.url, "--model", "fake", "--ollama-think", "true",
                "--solve-mode", solve_mode, "--text-tools", "--max-tokens", "1024", "--max-proposals", "1"]
        args += ["--auto-approve"] if approve else []
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(args + list(extra))
        return code, work, server, out.getvalue()

    def test_three_stages_verify_only_final_code_and_omit_all_thinking(self):
        code, work, server, output = self.run_cli([
            response(json.dumps({"name": "propose_plan", "arguments": PLAN})), response(GOOD), response("Fixed.")])
        self.assertEqual(code, 0, output)
        self.assertEqual((self.project / "calc.py").read_text(), BUGGY)
        self.assertEqual((work / "repo/calc.py").read_text(), GOOD)
        evidence = work / "evidence"
        for path in evidence.rglob("*"):
            if path.is_file():
                self.assertNotIn(PRIVATE.encode(), path.read_bytes(), path.name)
        messages = json.loads((evidence / "messages.json").read_text())
        self.assertEqual([m["stage"] for m in messages], ["intake", "solve", "respond"])
        self.assertEqual([r[1]["options"]["num_predict"] for r in server.requests if r[0] == "/api/chat"], [1024, 1024, 256])
        result = json.loads((evidence / "result.json").read_text())
        self.assertEqual(result["transport"]["think"], True)
        self.assertEqual(result["status"], "verified")

    def test_truncated_code_cannot_apply_and_truncated_response_is_incomplete(self):
        code, work, server, output = self.run_cli([
            response(json.dumps({"name": "propose_plan", "arguments": PLAN})),
            response(GOOD, reason="length"), response("Fixed", reason="length")])
        self.assertEqual(code, 1, output)
        result = json.loads((work / "evidence/result.json").read_text())
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(result["response_stage"]["status"], "incomplete")
        self.assertEqual((work / "evidence/patch.diff").read_text(), "")

    def test_truncated_plan_never_requests_approval_or_enters_solve(self):
        from unittest.mock import patch
        with patch("agentharness.__main__.cli_approver", side_effect=AssertionError("must not approve")):
            code, work, server, output = self.run_cli([
                response(calls=[native("propose_plan", PLAN)], reason="length")], approve=False,
                extra=("--plan-steps", "1"))
        self.assertEqual(code, 1, output)
        result = json.loads((work / "evidence/result.json").read_text())
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(result["changed_files"], [])

    def test_tools_mode_rejects_truncated_edit_before_apply(self):
        code, work, server, output = self.run_cli([
            response(calls=[native("propose_plan", PLAN)]),
            response(calls=[native("propose_edit", EDIT)], reason="length"), response("not fixed")],
            solve_mode="tools", extra=("--max-steps", "1"))
        self.assertEqual(code, 1, output)
        self.assertEqual((work / "evidence/patch.diff").read_text(), "")


class NativeSmokeScriptTests(unittest.TestCase):
    def smoke(self, *, running=True, installed=True, native_origin=True):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        bins = root / "bin"
        bins.mkdir()
        state = root / "running"
        if running:
            state.touch()
        (bins / "ollama").write_text('#!/bin/bash\necho "$*" >> "$SMOKE_CALLS"\n'
            'case "$1" in\nlist) test -f "$SMOKE_STATE" ;;\n'
            'serve) touch "$SMOKE_STATE" ;;\nshow) test "$SMOKE_INSTALLED" = yes ;;\n'
            '*) exit 99 ;;\nesac\n')
        (bins / "python").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$SMOKE_ARGS"\n')
        for p in bins.iterdir():
            p.chmod(0o755)
        env = {**os.environ, "PATH": str(bins) + os.pathsep + os.environ["PATH"],
               "SMOKE_CALLS": str(root / "calls"), "SMOKE_ARGS": str(root / "args"),
               "SMOKE_STATE": str(state), "SMOKE_INSTALLED": "yes" if installed else "no",
               "NESSA_RUNS_DIR": str(root / "runs"), "AGENT_MODEL": "explicit-model",
               "AGENT_BASE_URL": "http://127.0.0.1:11434" + ("" if native_origin else "/v1")}
        script = Path(__file__).resolve().parents[2] / "scripts/run_three_stage_smoke.sh"
        args = ["--transport", "ollama-native", "--ollama-think", "false"] if native_origin else []
        result = subprocess.run(["bash", str(script), *args], env=env, text=True,
                                capture_output=True, timeout=10)
        return result, root

    def test_native_origin_reuses_installed_server_without_download(self):
        result, root = self.smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "calls").read_text().splitlines(), ["list", "show explicit-model"])
        args = (root / "args").read_text().splitlines()
        self.assertEqual(args[args.index("--base-url") + 1], "http://127.0.0.1:11434")
        self.assertEqual(args[args.index("--ollama-think") + 1], "false")
        self.assertIn(str(root / "runs"), result.stdout)

    def test_native_origin_can_start_installed_loopback_server(self):
        result, root = self.smoke(running=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serve", (root / "calls").read_text().splitlines())
        self.assertIn(str(root / "runs/ollama.log"), result.stdout)
        self.assertNotIn("pull", (root / "calls").read_text())

    def test_missing_weights_stop_before_python_and_never_pull(self):
        result, root = self.smoke(installed=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((root / "args").exists())
        self.assertNotIn("pull", (root / "calls").read_text())
        self.assertIn("Pull it explicitly", result.stderr)

    def test_original_v1_script_path_is_unchanged(self):
        result, root = self.smoke(native_origin=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (root / "args").read_text().splitlines()
        self.assertEqual(args[args.index("--base-url") + 1], "http://127.0.0.1:11434/v1")
        self.assertNotIn("--transport", args)


if __name__ == "__main__":
    unittest.main()
