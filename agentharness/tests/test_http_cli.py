"""End to end over HTTP: real ChatClient + CLI against a fake OpenAI-compatible server."""
import io
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agentharness.__main__ import main
from agentharness.llm import ChatClient, ContextOverflow

SCRIPT = [
    ("search", {"pattern": "def add"}),
    ("read_file", {"path": "calc.py"}),
    ("propose_plan", {"goal": "fix add", "steps": ["replace - with +"]}),
    ("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"}),
    ("finish", {"summary": "fixed"}),
]


class FakeServer:
    def __init__(self, mode="native"):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "fake-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if mode == "overflow":
                    return self._send(400, {"error": "maximum context length exceeded"})
                n = sum(1 for m in body["messages"] if m["role"] == "assistant")
                name, args = SCRIPT[min(n, len(SCRIPT) - 1)]
                if mode == "text":  # Qwen-style tool call written as text
                    msg = {"role": "assistant", "content":
                           f'<tool_call>{json.dumps({"name": name, "arguments": args})}</tool_call>'}
                else:
                    msg = {"role": "assistant", "content": None, "reasoning_content": "thinking",
                           "tool_calls": [{"id": f"c{n}", "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]}
                self._send(200, {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                                 "usage": {"prompt_tokens": 50}})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HTTPEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def _run_cli(self, mode):
        server = FakeServer(mode)
        self.addCleanup(server.close)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["run", str(self.proj), "add() subtracts", "--auto-approve",
                         "--base-url", server.url, "--model", "fake-model",
                         "--work", str(self.tmp / "work")] + (["--text-tools"] if mode == "text" else []))
        return code, out.getvalue(), server

    def test_cli_native_tools(self):
        code, out, server = self._run_cli("native")
        self.assertEqual(code, 0, out)
        self.assertIn("Status: unverified", out)  # syntax passed; no tests exist to prove behaviour
        self.assertIn("tools", server.requests[0])
        patch = (self.tmp / "work" / "evidence" / "patch.diff").read_text()
        self.assertIn("+    return a + b", patch)
        self.assertIn("a - b", (self.proj / "calc.py").read_text(), "original untouched")

    def test_cli_text_tools(self):
        code, out, server = self._run_cli("text")
        self.assertEqual(code, 0, out)
        self.assertNotIn("tools", server.requests[0])

    def test_cli_resume_continues_http_agent(self):
        work = self.tmp / "resume-work"
        first_server = FakeServer("native")
        self.addCleanup(first_server.close)
        out1 = io.StringIO()
        with redirect_stdout(out1):
            first_code = main([
                "run", str(self.proj), "add() subtracts", "--auto-approve",
                "--base-url", first_server.url, "--model", "fake-model",
                "--work", str(work), "--max-steps", "1",
            ])
        self.assertEqual(first_code, 1)
        self.assertIn("Status: budget_exhausted", out1.getvalue())
        self.assertIn("a + b", (work / "repo" / "calc.py").read_text())

        second_server = FakeServer("native")
        self.addCleanup(second_server.close)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            second_code = main([
                "resume", str(work), "--auto-approve",
                "--base-url", second_server.url, "--model", "fake-model",
                "--max-steps", "4",
            ])
        self.assertEqual(second_code, 0, out2.getvalue())
        self.assertIn("Status: unverified", out2.getvalue())
        events = [json.loads(line) for line in
                  (work / "evidence" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["event"] == "start" for e in events), 1)
        self.assertEqual(sum(e["event"] == "resume" for e in events), 1)
        self.assertIn("a - b", (self.proj / "calc.py").read_text(),
                      "CLI resume must not modify the original project")

    def test_client_refuses_remote_and_reports_overflow(self):
        with self.assertRaises(ValueError):
            ChatClient("https://api.example.com/v1", "m")
        server = FakeServer("overflow")
        self.addCleanup(server.close)
        with self.assertRaises(ContextOverflow):
            ChatClient(server.url, "m").chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ChatClient(server.url, "m").models(), ["fake-model"])

    def test_cli_extract(self):
        (self.tmp / "a.txt").write_text("Invoice No: A-100\nTotal: $12.00\n")
        (self.tmp / "b.txt").write_text("Invoice No: B-200\nTotal: $3.50\n")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["extract", str(self.tmp / "a.txt"), str(self.tmp / "b.txt"),
                  "--field", "invoice:Invoice No=@id", "--field", "total:Total=@money",
                  "--out", str(self.tmp / "t.csv")])
        rows = (self.tmp / "t.csv").read_text().splitlines()
        self.assertEqual(rows[0].split(",")[2:4], ["invoice", "total"])
        self.assertTrue(rows[1].endswith("A-100,12.0,"))


if __name__ == "__main__":
    unittest.main()
