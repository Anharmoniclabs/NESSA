"""Offline tests for opt-in source-only solve transport; no live model calls."""
import ast
import json
import shutil
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentharness.checks import CheckResult, CheckRunner, syntax_check
from agentharness.code_proposals import extract_source, interface, parse_source, validate_interface
from agentharness.llm import Reply, ToolCall
from agentharness.three_stage import AtomicWorkspace, ThreeStageAgent, ThreeStageConfig
from agentharness.workspace import ToolError
from agentharness.tests.test_agent import BUGGY, UNITTEST, ScriptedClient, make_project

PLAN = {"goal": "Repair addition", "steps": ["Repair arithmetic", "Run tests"],
        "files": ["calc.py"], "checks": ["tests"]}
GOOD = BUGGY.replace("a - b", "a + b")


def plan(p=PLAN):
    return [("propose_plan", p)]


class SourceContractTests(unittest.TestCase):
    def test_raw_and_single_python_fence_preserve_source(self):
        for text in (GOOD, "```python\n" + GOOD + "```", "```\n" + GOOD + "```"):
            with self.subTest(text=text):
                self.assertEqual(extract_source(text, "calc.py"), GOOD)

    def test_invalid_wrappers_json_and_syntax_are_rejected(self):
        for text in ("", "Here is the code:\n```python\n" + GOOD + "```",
                     "```python\n" + GOOD + "```\nDone.",
                     "```python\n" + GOOD + "```\n```python\nx=1\n```",
                     '{"name":"write_file","arguments":{"path":"evil.py"}}',
                     "```json\n{}\n```", "def add(:\n return 2", "return 2"):
            with self.subTest(text=text), self.assertRaises(ToolError):
                extract_source(text, "calc.py")

    def test_interface_guards_names_arguments_defaults_annotations_and_decorators(self):
        original = interface(parse_source("@decorator\ndef f(x: int, y=2) -> int:\n    return x+y\n", "x.py"))
        for text in ("@decorator\ndef renamed(x: int, y=2) -> int:\n return x+y\n",
                     "@decorator\ndef f(z: int, y=2) -> int:\n return z+y\n",
                     "@decorator\ndef f(x: int, y=3) -> int:\n return x+y\n",
                     "@decorator\ndef f(x, y=2) -> int:\n return x+y\n",
                     "def f(x: int, y=2) -> int:\n return x+y\n"):
            with self.subTest(text=text), self.assertRaises(ToolError):
                validate_interface(text, "x.py", original)

    def test_class_methods_and_module_bindings_preserved_but_helpers_can_be_added(self):
        before = "import math\nVALUE=2\nclass A:\n def f(self,x):\n  return x\n"
        frozen = interface(parse_source(before, "x.py"))
        validate_interface(before + "def helper(x):\n return x+1\n", "x.py", frozen)
        for after in (before.replace("import math\n", ""), before.replace("VALUE=2\n", ""),
                      before.replace("f(self,x)", "f(self,x,y)")):
            with self.assertRaises(ToolError):
                validate_interface(after, "x.py", frozen)

    def test_duplicate_declarations_are_unsupported(self):
        with self.assertRaises(ToolError):
            interface(parse_source("def f(): pass\ndef f(): pass\n", "x.py"))

    def test_parse_and_compile_do_not_execute_source(self):
        source = "raise RuntimeError('must not execute during validation')\n" + GOOD
        self.assertEqual(extract_source(source, "calc.py"), source)

    def test_bindings_inside_module_control_flow_and_named_expressions_are_guarded(self):
        cases = [
            ("if True:\n    PUBLIC_VALUE=42\n", "PUBLIC_VALUE"),
            ("try:\n    import math as numeric\nexcept ImportError as problem:\n    fallback=1\n", "numeric"),
            ("for item in ():\n    VALUE=item\n", "item"),
            ("with resource() as handle:\n    pass\n", "handle"),
            ("match {'x': 1}:\n    case {'x': capture, **rest}:\n        pass\n", "capture"),
            ("(TOKEN := 42)\n", "TOKEN"),
        ]
        for prefix, binding in cases:
            with self.subTest(binding=binding):
                frozen = interface(parse_source(prefix + BUGGY, "calc.py"))
                self.assertIn(binding, frozen[1])
                with self.assertRaises(ToolError):
                    validate_interface(GOOD, "calc.py", frozen)

    def test_module_scope_collection_excludes_locals_and_comprehension_targets(self):
        source = ("VALUES=[i for i in range(3)]\nWALRUS=[(OUTER:=i) for i in range(3)]\n"
                  "def f():\n    local=1\n    return local\nclass C:\n    member=2\n")
        names = interface(parse_source(source, "x.py"))[1]
        self.assertTrue({"VALUES", "WALRUS", "OUTER", "f", "C"} <= names)
        self.assertTrue({"i", "local", "member"}.isdisjoint(names))

    def test_wildcard_import_scope_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(ToolError, "wildcard"):
            interface(parse_source("from math import *\n" + BUGGY, "calc.py"))

    def test_proposal_cannot_exceed_next_source_input_bound(self):
        with self.assertRaisesRegex(ToolError, "retry-input"):
            extract_source(GOOD + "#" + "x" * 12500, "calc.py")


class SourceOnlyFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = make_project(self.tmp)
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "work")

    def agent(self, steps, approver=None, checks=None, **over):
        self.client = ScriptedClient(steps)
        return ThreeStageAgent(self.client, self.ws,
            config=ThreeStageConfig(require_approval=approver is not None, solve_mode="code-only", **over),
            approver=approver, checks=checks or CheckRunner({"syntax": syntax_check, "tests": UNITTEST}, timeout=30))

    def test_valid_source_retains_three_stages_and_original(self):
        result = self.agent([plan(), GOOD, "The checks passed."]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(result.changed_files, ["calc.py"])
        self.assertEqual((self.project / "calc.py").read_text(), BUGGY)
        self.assertEqual(len(self.client.seen), 3)
        self.assertEqual(self.client.seen[1][1], [])
        self.assertNotIn("propose_edit(", self.client.seen[1][0][0]["content"])
        self.assertIn(BUGGY, self.client.seen[1][0][1]["content"])
        saved = json.loads((self.ws.work_dir / "evidence/result.json").read_text())
        self.assertEqual(saved["solve_mode"], "code-only")
        events = [json.loads(s) for s in (self.ws.work_dir / "evidence/events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "source_proposal" and e["signature_guard"] == "passed" for e in events))

    def test_changed_signature_is_rejected_then_can_be_corrected(self):
        wrong = "def add(x, b):\n    return x+b\n"
        result = self.agent([plan(), wrong, GOOD, "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(self.ws.journal), 1)
        self.assertIn("Preserve existing declarations", self.client.seen[2][0][1]["content"])

    def test_conditional_module_binding_cannot_disappear_in_verified_repair(self):
        original = "if True:\n    PUBLIC_VALUE=42\n" + BUGGY
        (self.project / "calc.py").write_text(original)
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "conditional-work")
        proper = original.replace("a - b", "a + b")
        result = self.agent([plan(), GOOD, proper, "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(self.ws.journal), 1)
        self.assertIn("PUBLIC_VALUE", (self.ws.repo / "calc.py").read_text())

    def test_oversized_candidate_rejected_before_apply_then_retry_succeeds(self):
        huge = BUGGY.replace("a - b", "a * b") + "#" + "x" * 12500
        result = self.agent([plan(), huge, GOOD, "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(len(self.ws.journal), 1)
        self.assertIn("retry-input", self.client.seen[2][0][1]["content"])

    def test_accepted_proposals_must_leave_bounded_feedback_space(self):
        candidate = GOOD + "#" + "x" * 1000
        result = self.agent([plan(), candidate, GOOD, "done"], context_chars=8000).run("Fix addition. " + "x" * 6800)
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(len(self.ws.journal), 1)
        self.assertIn("correction-safe", self.client.seen[2][0][1]["content"])
        for messages, tools in self.client.seen[1:3]:
            self.assertLessEqual(sum(len(m["content"]) for m in messages), 8000)

    def test_verification_failure_returns_current_source_for_repair(self):
        bad = BUGGY.replace("a - b", "a * b")
        result = self.agent([plan(), bad, GOOD, "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(len(self.ws.journal), 2)
        second = self.client.seen[2][0][1]["content"]
        self.assertIn("a * b", second)
        self.assertIn("AssertionError", second)

    def test_unchanged_raw_or_fenced_source_never_succeeds(self):
        for text in (BUGGY, "```python\n" + BUGGY + "```"):
            with self.subTest(text=text):
                self.ws = AtomicWorkspace.create(self.project, self.tmp / ("fence" if text.startswith("```") else "raw"))
                result = self.agent([plan(), text], max_proposals=1).run("Fix addition")
                self.assertEqual(result.status, "no_change")
                self.assertEqual(result.patch, "")
                self.assertIn("failed", result.checks["final"]["tests"])

    def test_invalid_generation_is_bounded_without_apply(self):
        result = self.agent([plan(), "def add(:"], max_proposals=2).run("Fix addition")
        self.assertEqual(result.status, "no_change")
        self.assertEqual(result.steps, 2)
        self.assertEqual(self.ws.journal, [])

    def test_native_tool_response_cannot_mutate_any_path(self):
        result = self.agent([plan(), [("write_file", {"path": "tests/test_calc.py", "content": ""})]],
                            max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "no_change")
        self.assertEqual(result.patch, "")
        self.assertEqual(self.ws.journal, [])

    def test_tool_shaped_literal_inside_python_is_data_and_never_dispatched(self):
        source = GOOD + '\nmetadata = {"name": "write_file", "arguments": {"path": "tests/test_calc.py", "content": ""}}\n'
        tests = (self.ws.repo / "tests/test_calc.py").read_bytes()
        result = self.agent([plan(), source, "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual((self.ws.repo / "tests/test_calc.py").read_bytes(), tests)
        self.assertEqual(result.changed_files, ["calc.py"])

    def test_external_change_during_model_call_is_not_overwritten(self):
        def race(messages):
            (self.ws.repo / "calc.py").write_text(BUGGY + "# external\n")
            return GOOD
        result = self.agent([plan(), race, "done"], max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "error")
        self.assertEqual(self.ws.journal, [])
        self.assertEqual((self.ws.repo / "calc.py").read_text(), BUGGY + "# external\n")

    def test_rejected_approval_never_enters_source_solve(self):
        result = self.agent([plan(), GOOD], approver=lambda p: (False, "")).run("Fix addition")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.patch, "")
        self.assertEqual(result.steps, 0)

    def test_direct_solve_without_approval_is_rejected(self):
        agent = self.agent([GOOD])
        agent.approved_files = {"calc.py"}
        with self.assertRaisesRegex(ToolError, "approval"):
            agent._solve_code_only()
        self.assertEqual(self.client.seen, [])

    def test_multiple_approved_files_refused_before_source_generation(self):
        (self.project / "other.py").write_text("value=1\n")
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "two-work")
        result = self.agent([plan({**PLAN, "files": ["calc.py", "other.py"]}), GOOD]).run("Fix addition")
        self.assertEqual(result.status, "error")
        self.assertIn("exactly one", result.summary)
        self.assertEqual(result.steps, 0)
        self.assertEqual(result.patch, "")

    def test_non_python_file_refused_without_source_generation(self):
        (self.project / "notes.txt").write_text("a note\n")
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "txt-work")
        result = self.agent([plan({**PLAN, "files": ["notes.txt"]}), GOOD]).run("Fix notes")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.patch, "")

    def test_large_source_refused_without_generation(self):
        (self.project / "calc.py").write_text(BUGGY + "#" + "x" * 12000)
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "large-work")
        result = self.agent([plan(), GOOD]).run("Fix addition")
        self.assertEqual(result.status, "error")
        self.assertIn("source bound", result.summary)
        self.assertEqual(result.steps, 0)

    def test_syntax_broken_original_is_explicitly_unsupported_without_generation(self):
        (self.project / "calc.py").write_text("def add(a,b)\n    return a-b\n")
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "syntax-original-work")
        result = self.agent([plan(), GOOD]).run("Fix addition syntax")
        self.assertEqual(result.status, "error")
        self.assertIn("syntactically valid original", result.summary)
        self.assertIn("tools mode", result.summary)
        self.assertEqual(result.steps, 0)
        self.assertEqual(self.ws.journal, [])

    def test_truncated_but_parseable_response_is_rejected(self):
        agent = self.agent([plan(), GOOD, "done"], max_proposals=1)
        original = self.client.chat
        def chat(*args):
            reply = original(*args)
            if len(self.client.seen) == 2:
                reply.finish_reason = "length"
            return reply
        self.client.chat = chat
        result = agent.run("Fix addition")
        self.assertEqual(result.status, "no_change")
        self.assertEqual(result.patch, "")

    def test_expired_model_output_cannot_apply(self):
        def delayed(messages):
            time.sleep(0.06)
            return GOOD
        result = self.agent([plan(), delayed, "done"], time_budget=0.04,
            checks=CheckRunner({"syntax": syntax_check, "tests": lambda ws: CheckResult("tests", "failed")})).run("Fix addition")
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.patch, "")

    def test_generated_check_mutation_is_still_rejected_and_isolated(self):
        tests = (self.ws.repo / "tests/test_calc.py").read_bytes()
        source = "from pathlib import Path\nPath('tests/test_calc.py').write_text('# disabled')\n" + GOOD
        result = self.agent([plan(), source], max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("error", result.checks["final"]["tests"])
        self.assertEqual((self.ws.repo / "tests/test_calc.py").read_bytes(), tests)

    def test_cli_http_three_stages_use_no_solve_tools(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                if len(requests) == 1:
                    message = {"content": "", "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "propose_plan", "arguments": json.dumps(PLAN)}}]}
                else:
                    message = {"content": GOOD if len(requests) == 2 else "The checks passed."}
                payload = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            run = subprocess.run([sys.executable, "-m", "agentharness.three_stage", str(self.project),
                "Fix addition", "--work", str(self.tmp / "cli-code-work"), "--auto-approve",
                "--solve-mode", "code-only", "--base-url", f"http://127.0.0.1:{server.server_port}/v1",
                "--check", f"tests={UNITTEST}"], capture_output=True, text=True, timeout=30)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(len(requests), 3)
        self.assertIn("tools", requests[0])
        self.assertNotIn("tools", requests[1])
        self.assertNotIn("tools", requests[2])
        self.assertEqual((self.project / "calc.py").read_text(), BUGGY)


if __name__ == '__main__':
    unittest.main()
