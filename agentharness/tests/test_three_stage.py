"""State-machine and HTTP integration tests; these are not live-model benchmarks."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentharness.checks import CheckResult, CheckRunner, syntax_check
from agentharness.llm import ChatClient, ModelError
from agentharness.three_stage import AtomicWorkspace, ThreeStageAgent, ThreeStageConfig, READ_TOOLS
from agentharness.workspace import ToolError
from agentharness.tests.test_agent import ScriptedClient, make_project, BUGGY, UNITTEST

PLAN = {"goal": "Repair addition", "steps": ["Correct arithmetic"],
        "files": ["calc.py"], "checks": ["tests"]}
EDIT = {"path": "calc.py", "old": "a - b", "new": "a + b"}
BLOCK = {"reason": "Tests passed; task already correct", "evidence": "The proposed fix was correct"}


def call(name, args):
    return [(name, args)]


class ThreeStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = make_project(self.tmp)
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "work")

    def agent(self, steps, native=True, checks=None, approver=None, **cfg):
        client = ScriptedClient(steps, native=native)
        client.max_tokens = 768
        self.client = client
        return ThreeStageAgent(client, self.ws,
            config=ThreeStageConfig(require_approval=approver is not None, max_steps=cfg.pop("max_steps", 5), **cfg),
            checks=checks or CheckRunner({"syntax": syntax_check, "tests": UNITTEST}, timeout=30),
            approver=approver)

    def test_native_happy_path_has_three_stages_and_real_checks(self):
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT),
                             "Changed calc.py; tests passed."]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(result.changed_files, ["calc.py"])
        self.assertIn("passed", result.checks["final"]["tests"])
        self.assertEqual((self.project / "calc.py").read_text(), BUGGY)
        events = [json.loads(s) for s in (self.ws.work_dir / "evidence/events.jsonl").read_text().splitlines()]
        self.assertEqual([e["phase"] for e in events if e["event"] == "stage"],
                         ["intake", "solve", "respond"])
        self.assertEqual(len(self.ws.journal), 1)
        self.assertTrue((self.ws.work_dir / "evidence/checkpoints/edit-0001.diff").is_file())
        self.assertEqual(len(self.client.seen), 3)
        self.assertEqual(self.client.seen[-1][1], [])
        self.assertEqual(self.client.max_tokens, 768)

    def test_text_mode_schemas_have_full_arguments_and_same_gates(self):
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT), "done"],
                            native=False, tool_mode="text").run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        solve_prompt = self.client.seen[1][0][0]["content"]
        for arg in ('"path"', '"old"', '"new"'):
            self.assertIn(arg, solve_prompt)
        self.assertNotIn('"name":"finish"', solve_prompt)
        self.assertNotIn("replace_in_file", solve_prompt)

    def test_observed_trace_cannot_finish_or_mutate_during_intake(self):
        premature = {"path": "calc.py", "start": 1, "end": 2,
                     "replacement": "def add(a, b): return a + b"}
        result = self.agent([call("read_file", {"path": "calc.py"}),
            call("replace_in_file", premature), call("propose_plan", PLAN),
            call("finish", {"summary": "Modified add; tests passed", "no_change_reason": "already correct"}),
            call("finish", {"summary": "Already correct"}), call("propose_edit", EDIT),
            "All done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertEqual(len(self.ws.journal), 1)
        self.assertEqual(result.steps, 3)
        first_solve = self.client.seen[3][0]
        serialized = json.dumps(first_solve)
        self.assertNotIn("replacement", serialized)
        self.assertIn("return a - b", serialized)
        self.assertIn('"applied_edits": 0', first_solve[1]["content"])
        self.assertNotIn("finish", self.client.seen[3][1])

    def test_repeated_false_finish_is_bounded_failure(self):
        result = self.agent([call("propose_plan", PLAN), call("finish", {"summary": "tests passed"})],
                            max_steps=3).run("Fix addition")
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.patch, "")
        self.assertIn("failed", result.checks["final"]["tests"])
        self.assertNotIn("tests passed", result.summary)

    def test_false_blocker_never_becomes_success(self):
        result = self.agent([call("propose_plan", PLAN), call("report_blocker", BLOCK),
                             "Successfully fixed everything; tests passed"]).run("Fix addition")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.patch, "")
        self.assertIn("failed", result.checks["final"]["tests"])
        self.assertNotIn("Successfully", result.summary)
        saved = json.loads((self.ws.work_dir / "evidence/result.json").read_text())
        self.assertFalse(saved["response_stage"]["authoritative"])

    def test_scope_and_test_edits_denied_before_effect(self):
        (self.project / "other.py").write_text("value = 1\n")
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "work")
        originals = {n: (self.ws.repo / n).read_bytes() for n in ("other.py", "tests/test_calc.py")}
        result = self.agent([call("propose_plan", PLAN),
            call("propose_edit", {"path": "other.py", "old": "1", "new": "2"}),
            call("propose_edit", {"path": "tests/test_calc.py", "old": "5", "new": "-1"}),
            call("propose_edit", EDIT), "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        for n, before in originals.items():
            self.assertEqual((self.ws.repo / n).read_bytes(), before)
        self.assertEqual([j["path"] for j in self.ws.journal], ["calc.py"])

    def test_protected_plan_is_rejected_before_approval(self):
        approvals = []
        plan = {**PLAN, "files": ["tests/test_calc.py"]}
        result = self.agent([call("propose_plan", plan)], plan_steps=1,
            approver=lambda p: (approvals.append(p) or True, "")).run("Fix addition")
        self.assertEqual(result.status, "no_plan")
        self.assertEqual(approvals, [])
        self.assertEqual(result.patch, "")

    def test_denied_approval_prevents_solve_and_edits(self):
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT)],
                            approver=lambda p: (False, "")).run("Fix addition")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.patch, "")
        self.assertNotIn("propose_edit", self.client.seen[-1][1])

    def test_read_menu_excludes_side_effect_capabilities(self):
        self.agent([call("propose_plan", PLAN), call("report_blocker", BLOCK)]).run("Fix addition")
        for _, tools in self.client.seen:
            for forbidden in ("extract_table", "run_command", "dev_start", "replace_in_file", "finish"):
                self.assertNotIn(forbidden, tools)
        self.assertEqual(set(READ_TOOLS), {"read_file", "list_dir", "outline", "search", "instructions_for"})

    def test_failing_edit_returns_current_source_and_test_output_to_solve(self):
        bad = {**EDIT, "new": "a * b"}
        fixed = {**EDIT, "old": "a * b"}
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", bad),
                             call("propose_edit", fixed), "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified", result.summary)
        packet = json.loads(self.client.seen[2][0][1]["content"])
        self.assertEqual(packet["actual_state"]["applied_edits"], 1)
        self.assertIn("return a * b", json.dumps(packet["current_observations"]))
        self.assertIn("AssertionError", packet["last_outcome"])

    def test_proposal_budget_stops_failed_checks(self):
        result = self.agent([call("propose_plan", PLAN),
                            call("propose_edit", {**EDIT, "new": "a * b"})],
                            max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("failed", result.checks["final"]["tests"])

    def test_noop_and_ambiguous_old_rejected(self):
        result = self.agent([call("propose_plan", PLAN),
            call("propose_edit", {**EDIT, "new": EDIT["old"]}),
            call("propose_edit", {**EDIT, "old": "a", "new": "z"}),
            call("propose_edit", EDIT), "done"]).run("Fix addition")
        self.assertEqual(result.status, "verified")
        self.assertEqual(len(self.ws.journal), 1)

    def test_wrong_edit_parameter_names_do_not_mutate(self):
        result = self.agent([call("propose_plan", PLAN),
            call("propose_edit", {"path": "calc.py", "start": 1, "end": 2, "replacement": "x"}),
            call("report_blocker", BLOCK)]).run("Fix addition")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.patch, "")

    def test_multiple_calls_are_rejected_as_whole_turn(self):
        result = self.agent([call("propose_plan", PLAN),
            [("propose_edit", EDIT), ("report_blocker", BLOCK)],
            call("report_blocker", BLOCK)]).run("Fix addition")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.ws.journal, [])

    def test_stale_read_rejected(self):
        agent = self.agent([call("propose_plan", PLAN)])
        agent.approved_files = {"calc.py"}
        agent._refresh_sources()
        (self.ws.repo / "calc.py").write_text(BUGGY + "# external change\n")
        with self.assertRaisesRegex(ToolError, "changed since"):
            agent._apply(EDIT)
        self.assertEqual(self.ws.journal, [])

    def test_symlink_and_path_escape_rejected(self):
        agent = self.agent([call("propose_plan", PLAN)])
        (self.ws.repo / "alias.py").symlink_to("calc.py")
        for name in ("alias.py", "../proj/calc.py", "/etc/passwd", "./calc.py", "x/../calc.py"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                agent._path(name)

    def test_atomic_write_failure_preserves_original(self):
        self.ws.read("calc.py")
        with patch("agentharness.three_stage.os.replace", side_effect=OSError("disk problem")):
            with self.assertRaises(OSError):
                self.ws.replace("calc.py", "a - b", "a + b")
        self.assertEqual((self.ws.repo / "calc.py").read_text(), BUGGY)
        self.assertEqual(self.ws.journal, [])
        self.assertEqual(list(self.ws.repo.glob(".three-stage-*")), [])

    def test_invalid_plan_arrays_rejected(self):
        agent = self.agent([call("propose_plan", PLAN)])
        for field in ("steps", "files", "checks"):
            for value in ([], [123], [""]):
                with self.subTest(field=field, value=value), self.assertRaises(ToolError):
                    agent._validate_plan({**PLAN, field: value})

    def test_nonpassing_check_statuses_never_verify(self):
        for status in ("failed", "timeout", "error", "setup_error", "no_tests"):
            with self.subTest(status=status):
                agent = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT)],
                    checks=CheckRunner({"tests": lambda ws, s=status: CheckResult("tests", s)}),
                    max_proposals=1)
                # New workspace for every case, before the proposed replacement.
                agent.ws = AtomicWorkspace.create(self.project, self.tmp / f"case-{status}")
                result = agent.run("Fix addition")
                self.assertNotEqual(result.status, "verified")

    def test_missing_required_check_cannot_be_dropped_by_plan(self):
        agent = self.agent([call("propose_plan", {**PLAN, "checks": ["syntax"]}),
                            call("propose_edit", EDIT)],
            checks=CheckRunner({"syntax": syntax_check}), max_proposals=1)
        result = agent.run("Fix addition")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("setup_error", result.checks["final"]["tests"])

    def test_model_backend_error_is_honest_and_still_records_respond_attempt(self):
        agent = self.agent([call("propose_plan", PLAN)])
        def fail(*args):
            raise ModelError("HTTP 400: tool schema unsupported")
        agent.client.chat = fail
        result = agent.run("Fix addition")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.patch, "")
        self.assertIn("tool schema unsupported", result.summary)
        self.assertEqual(agent.respond["status"], "error")

    def test_response_tool_call_cannot_edit(self):
        malicious = {**EDIT, "old": "a + b", "new": "a / b"}
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT),
                             call("propose_edit", malicious)]).run("Fix addition")
        self.assertEqual(result.status, "verified")
        self.assertIn("return a + b", (self.ws.repo / "calc.py").read_text())
        self.assertEqual(len(self.ws.journal), 1)

    def test_handoff_does_not_retain_rejected_plan_edit(self):
        result = self.agent([call("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"}),
            call("propose_plan", PLAN), call("report_blocker", BLOCK)]).run("Fix addition")
        self.assertEqual(result.status, "blocked")
        solve = json.loads(self.client.seen[2][0][1]["content"])
        self.assertEqual(solve["actual_state"]["applied_edits"], 0)
        self.assertNotIn("replace_in_file", json.dumps(solve))

    def test_native_text_mixed_offered_and_forbidden_calls_reject_whole_turn(self):
        for native in (False, True):
            with self.subTest(native=native):
                self.ws = AtomicWorkspace.create(self.project, self.tmp / f"mixed-{native}")
                result = self.agent([call("propose_plan", PLAN),
                    [("finish", {"summary": "done"}), ("propose_edit", EDIT)]],
                    native=native, max_steps=1, tool_mode="native" if native else "text").run("Fix addition")
                self.assertEqual(result.status, "budget_exhausted")
                self.assertEqual(result.patch, "")

    def test_test_mutation_from_candidate_code_is_rejected_and_isolated(self):
        malicious = {**EDIT, "old": BUGGY, "new":
            "from pathlib import Path\nPath('tests/test_calc.py').write_text('# removed tests')\n"
            "def add(a, b):\n    return a + b\n"}
        original_test = (self.ws.repo / "tests/test_calc.py").read_bytes()
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", malicious)],
                            max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("error", result.checks["final"]["tests"])
        self.assertEqual((self.ws.repo / "tests/test_calc.py").read_bytes(), original_test)
        self.assertEqual(result.changed_files, ["calc.py"])

    def test_source_mutation_from_registered_check_is_rejected(self):
        def mutating_check(ws):
            (ws.repo / "calc.py").write_text("broken = True\n")
            return CheckResult("tests", "passed")
        result = self.agent([call("propose_plan", PLAN), call("propose_edit", EDIT)],
            checks=CheckRunner({"syntax": syntax_check, "tests": mutating_check}),
            max_proposals=1).run("Fix addition")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("a + b", (self.ws.repo / "calc.py").read_text())

    def test_unexpected_working_copy_changes_cannot_be_blessed_by_next_proposal(self):
        agent = self.agent([call("propose_plan", PLAN)])
        agent.approved_files = {"calc.py"}
        agent._refresh_sources()
        (self.ws.repo / "surprise.py").write_text("x=1\n")
        with self.assertRaisesRegex(ToolError, "authorized state"):
            agent._apply(EDIT)
        self.assertEqual(self.ws.journal, [])

    def test_supported_instruction_and_test_names_are_protected(self):
        for name in ("AGENT.md", ".agent/instructions.md", "test.py", "testcalc.py",
                     "src/widget.spec.tsx", "jest.config.js", "vitest.config.ts"):
            with self.subTest(name=name):
                self.assertTrue(ThreeStageAgent._protected(name))

    def test_delayed_solve_response_cannot_apply_after_budget(self):
        def delayed(messages):
            time.sleep(0.06)
            return call("propose_edit", EDIT)
        agent = self.agent([call("propose_plan", PLAN), delayed, "done"], time_budget=0.04,
                          checks=CheckRunner({"syntax": syntax_check,
                              "tests": lambda ws: CheckResult("tests", "passed")}))
        result = agent.run("Fix addition")
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.patch, "")

    def test_context_bound_rejects_oversized_required_facts(self):
        agent = self.agent([call("propose_plan", PLAN)], context_chars=8000)
        agent.feedback = "x" * 10000
        agent.task = "x" * 10000
        with self.assertRaisesRegex(ModelError, "context bound"):
            agent._ask_stage(["propose_plan"])

    def test_null_byte_path_is_recoverable_tool_error(self):
        agent = self.agent([call("propose_plan", PLAN)])
        with self.assertRaises(ToolError):
            agent._path("bad\x00.py")

    def test_ignored_files_cannot_be_approved_or_applied(self):
        for folder in ("build", "dist", ".venv"):
            (self.project / folder).mkdir()
            (self.project / folder / "module.py").write_text("x=1\n")
        self.ws = AtomicWorkspace.create(self.project, self.tmp / "ignored-work")
        agent = self.agent([call("propose_plan", PLAN)])
        for folder in ("build", "dist", ".venv"):
            with self.subTest(folder=folder), self.assertRaisesRegex(ToolError, "exportable"):
                agent._validate_plan({**PLAN, "files": [folder + "/module.py"]})


class HTTPIntegration(unittest.TestCase):
    def test_cli_round_trip_native_and_text(self):
        for text_mode in (False, True):
            with self.subTest(text_mode=text_mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = make_project(root)
                requests = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_POST(self):
                        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        requests.append(body)
                        index = len(requests)
                        if index < 3:
                            name, args = ("propose_plan", PLAN) if index == 1 else ("propose_edit", EDIT)
                            message = ({"content": json.dumps({"name": name, "arguments": args})} if text_mode else
                                {"content": "", "tool_calls": [{"id": "1", "type": "function", "function":
                                 {"name": name, "arguments": json.dumps(args)}}]})
                        else:
                            message = {"content": "The supplied evidence says calc.py changed and tests passed."}
                        payload = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    cmd = [sys.executable, "-m", "agentharness.three_stage", str(project), "Fix addition",
                           "--work", str(root / "run"), "--auto-approve", "--base-url",
                           f"http://127.0.0.1:{server.server_port}/v1", "--check", f"tests={UNITTEST}"]
                    if text_mode:
                        cmd.append("--text-tools")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(requests), 3)
                self.assertNotIn("tools", requests[-1])
                self.assertEqual(requests[-1]["max_tokens"], 256)
                result_json = json.loads((root / "run/evidence/result.json").read_text())
                self.assertEqual(result_json["status"], "verified")
                self.assertEqual(result_json["mode"], "three-stage-v1")
                self.assertEqual((project / "calc.py").read_text(), BUGGY)


if __name__ == "__main__":
    unittest.main()
