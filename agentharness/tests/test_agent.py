"""Controller, workspace, checks and batch tests with a scripted (fake) model."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from agentharness.agent import Agent, AgentConfig
from agentharness.batch import run_batch
from agentharness.checks import CheckRunner, classify, parse_counts, run_command
from agentharness.llm import Reply, ToolCall, parse_text_tool_calls
from agentharness.policy import apply_policy
from agentharness.workspace import ToolError, Workspace

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = ("import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
UNITTEST = f'"{sys.executable}" -m unittest discover -q'


class ScriptedClient:
    """Plays back tool calls. Each step: list of (name, args), a str (plain text reply),
    or a callable(messages) -> step. Repeats the last step when the script runs out."""
    model = "scripted"

    def __init__(self, steps, native=True):
        self.steps = list(steps)
        self.native = native
        self.seen = []

    def chat(self, messages, tools=None, tool_names=None):
        self.seen.append((json.loads(json.dumps(messages)), [t["function"]["name"] for t in tools or []]))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            calls = parse_text_tool_calls(step, tool_names)
            return Reply(step, calls, False)
        if not self.native:
            text = "\n".join(json.dumps({"name": n, "arguments": a}) for n, a in step)
            return Reply(text, parse_text_tool_calls(text, tool_names), False)
        raw = [{"id": f"c{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(step)]
        calls = [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(step)]
        return Reply("", calls, True, raw, prompt_tokens=100)


def make_project(root: Path) -> Path:
    proj = root / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "calc.py").write_text(BUGGY)
    (proj / "tests" / "__init__.py").write_text("")
    (proj / "tests" / "test_calc.py").write_text(TEST)
    return proj


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = make_project(self.tmp)
        self.ws = Workspace.create(self.proj, self.tmp / "work")

    def agent(self, steps, native=True, **cfg):
        from agentharness.checks import syntax_check
        checks = CheckRunner({"syntax": syntax_check, "tests": UNITTEST}, timeout=60)
        config = AgentConfig(require_approval=False, **cfg)
        self.client = ScriptedClient(steps, native)
        return Agent(self.client, self.ws, config=config, checks=checks)


class AgentFlow(Base):
    def test_described_actions_do_not_execute_or_verify(self):
        result = self.agent([
            [("read_file", {"path": "calc.py"})],
            [("propose_plan", {"goal": "fix add", "steps": ["use +"], "files": ["calc.py"]})],
            json.dumps({"steps": [{"action": "replace_in_file", "path": "calc.py",
                                   "old": "a - b", "new": "a + b"}],
                        "summary": "File edited and tests passed."}),
        ]).run("Fix add")
        self.assertEqual(result.status, "stalled")
        self.assertEqual(result.changed_files, [])
        self.assertEqual((self.ws.repo / "calc.py").read_text(), BUGGY)
        self.assertEqual((self.proj / "calc.py").read_text(), BUGGY)

    def test_plan_edit_verify(self):
        result = self.agent([
            [("search", {"pattern": "def add"})],
            [("read_file", {"path": "calc.py"})],
            [("propose_plan", {"goal": "fix add", "steps": ["use +"], "files": ["calc.py"]})],
            [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})],
            [("finish", {"summary": "add now adds"})],
        ]).run("add() subtracts")
        self.assertEqual(result.status, "verified", result.summary)
        self.assertIn("+    return a + b", result.patch)
        self.assertEqual(result.changed_files, ["calc.py"])
        self.assertIn("failed", result.checks["baseline"]["tests"])
        self.assertIn("passed", result.checks["final"]["tests"])
        self.assertEqual((self.proj / "calc.py").read_text(), BUGGY, "original must be untouched")
        # read-only tools only during planning
        plan_tools = self.client.seen[0][1]
        self.assertNotIn("replace_in_file", plan_tools)
        self.assertNotIn("finish", plan_tools)
        events = [json.loads(l)["event"] for l in (Path(result.evidence_dir) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[0], "start")
        self.assertEqual(events[-1], "end")
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
            r = subprocess.run(["git", "apply", "--check", str(Path(result.evidence_dir) / "patch.diff")],
                               cwd=self.proj, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_failed_verification_sends_agent_back(self):
        result = self.agent([
            [("read_file", {"path": "calc.py"})],
            [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a * b"})],
            [("finish", {"summary": "done"})],
            [("replace_in_file", {"path": "calc.py", "old": "a * b", "new": "a + b"})],
            [("finish", {"summary": "really done"})],
        ], plan_first=False).run("fix add")
        self.assertEqual(result.status, "verified")
        tool_msgs = [m["content"] for m in self.client.seen[-1][0] if m["role"] == "tool"]
        self.assertTrue(any(c.startswith("Not accepted: verification failed") for c in tool_msgs))

    def test_failing_checks_after_retries(self):
        result = self.agent([
            [("read_file", {"path": "calc.py"})],
            [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a * b"})],
            [("finish", {"summary": "done"})],
        ], plan_first=False, finish_retries=1).run("fix add")
        self.assertEqual(result.status, "failed_checks")

    def test_syntax_only_is_unverified_not_success(self):
        (self.ws.repo / "tests" / "test_calc.py").unlink()
        result = self.agent([
            [("read_file", {"path": "calc.py"})],
            [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})],
            [("finish", {"summary": "done"})],
        ], plan_first=False, baseline_checks=False, verify=("syntax", "tests")).run("fix add")
        self.assertEqual(result.status, "unverified")

    def test_repeated_reads_stall(self):
        result = self.agent([[("read_file", {"path": "calc.py"})]], plan_first=False,
                            explore_budget=3).run("fix add")
        self.assertEqual(result.status, "stalled")
        contents = [m["content"] for m in self.client.seen[-1][0]]
        self.assertTrue(any("duplicate" in c for c in contents))
        self.assertTrue(any("without changing anything" in c for c in contents))

    def test_no_tool_calls_stalls(self):
        result = self.agent(["I think the fix is easy."], plan_first=False).run("fix add")
        self.assertEqual(result.status, "stalled")

    def test_text_mode_tool_calls(self):
        result = self.agent([
            [("read_file", {"path": "calc.py"})],
            [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})],
            [("finish", {"summary": "ok"})],
        ], native=False, plan_first=False, tool_mode="text").run("fix add")
        self.assertEqual(result.status, "verified")
        self.assertIn("Available tools", self.client.seen[0][0][0]["content"])

    def test_bad_arguments_are_reported_to_model(self):
        result = self.agent([
            [("read_file", {"file": "calc.py"})],
            [("finish", {"summary": "gave up"})],
        ], plan_first=False).run("fix add")
        self.assertEqual(result.status, "no_change")
        tool_msgs = [m["content"] for m in self.client.seen[-1][0] if m["role"] == "tool"]
        self.assertTrue(tool_msgs[0].startswith("ERROR: Missing required argument"))

    def test_rejected_plan_changes_nothing(self):
        checks = CheckRunner({"tests": UNITTEST})
        client = ScriptedClient([[("propose_plan", {"goal": "g", "steps": ["s"]})]])
        agent = Agent(client, self.ws, config=AgentConfig(replan_limit=0), checks=checks,
                      approver=lambda plan: (False, ""))
        result = agent.run("fix add")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.patch, "")

    def test_policy_narrows_but_cannot_add(self):
        class P:
            mode, top_k = "active", 1
            def rank(self, state, permitted):
                return ["delete_everything", "read_file", "search"]
        offered, ranking = apply_policy(P(), {}, ["read_file", "search", "finish"])
        self.assertEqual(ranking, ["read_file", "search"])
        self.assertEqual(offered, ["read_file", "finish"])


class WorkspaceSafety(Base):
    def test_edit_requires_fresh_read(self):
        with self.assertRaises(ToolError):
            self.ws.replace("calc.py", "a - b", "a + b")
        self.ws.read("calc.py")
        (self.ws.repo / "calc.py").write_text(BUGGY + "# changed by a command\n")
        with self.assertRaisesRegex(ToolError, "changed since"):
            self.ws.replace("calc.py", "a - b", "a + b")

    def test_path_escape_refused(self):
        for bad in ("../proj/calc.py", "/etc/passwd"):
            with self.assertRaises(ToolError):
                self.ws.read(bad)

    def test_not_found_gives_hint(self):
        self.ws.read("calc.py")
        with self.assertRaisesRegex(ToolError, "Closest line 2"):
            self.ws.replace("calc.py", "return a -  b", "x")

    def test_edit_lines_and_undo(self):
        self.ws.read("calc.py")
        self.ws.edit_lines("calc.py", 2, 2, "    return a + b")
        self.assertIn("a + b", (self.ws.repo / "calc.py").read_text())
        self.ws.undo("calc.py")
        self.assertEqual(self.ws.patch(), "")

    def test_patch_new_deleted_and_no_newline(self):
        self.ws.write("new.txt", "no newline")
        (self.ws.repo / "tests" / "__init__.py").unlink()
        self.ws.read("calc.py")
        self.ws.write("calc.py", "def add(a, b):\n    return a + b")
        patch = self.ws.patch()
        self.assertIn("new file mode", patch)
        self.assertIn("deleted file mode", patch)
        self.assertIn("\\ No newline at end of file", patch)
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
            (self.tmp / "p.diff").write_text(patch)
            r = subprocess.run(["git", "apply", str(self.tmp / "p.diff")], cwd=self.proj,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((self.proj / "new.txt").read_text(), "no newline")
            self.assertFalse((self.proj / "tests" / "__init__.py").exists())

    def test_outline_and_search(self):
        self.assertIn("def add  lines 1-2", self.ws.outline("calc.py"))
        self.assertIn("calc.py:1:", self.ws.search(r"def \w+", glob="*.py"))
        self.assertEqual(self.ws.search("zzz_nothing"), "no matches")


class Checks(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify("python -m pytest", 0, "", False), "passed")
        self.assertEqual(classify("python -m pytest", 1, "1 failed", False), "failed")
        self.assertEqual(classify("python -m pytest", 5, "", False), "no_tests")
        self.assertEqual(classify("python -m pytest", 2, "ERROR collecting", False), "setup_error")
        self.assertEqual(classify("x", None, "", True), "timeout")
        self.assertEqual(classify("foo", 127, "foo: command not found", False), "setup_error")
        self.assertEqual(classify("python -m unittest", 0, "Ran 0 tests", False), "no_tests")

    def test_counts(self):
        self.assertEqual(parse_counts("== 1 failed, 319 passed in 3s =="), {"passed": 319, "failed": 1})
        c = parse_counts("Ran 5 tests in 0.1s\n\nFAILED (failures=1, errors=1)")
        self.assertEqual(c, {"failed": 1, "errors": 1, "passed": 3})

    def test_timeout_and_output_limit(self):
        code, out, timed_out = run_command(f'"{sys.executable}" -c "import time; time.sleep(10)"',
                                           Path.cwd(), timeout=1)
        self.assertTrue(timed_out)
        self.assertIsNone(code)
        code, out, _ = run_command(f'"{sys.executable}" -c "print(\'x\' * 50000)"', Path.cwd(),
                                   output_limit=1000)
        self.assertEqual(code, 0)
        self.assertLess(len(out), 1200)
        self.assertIn("omitted", out)


class TextToolCalls(unittest.TestCase):
    def test_formats(self):
        calls = parse_text_tool_calls('<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>')
        self.assertEqual((calls[0].name, calls[0].arguments), ("read_file", {"path": "a"}))
        calls = parse_text_tool_calls('Sure:\n```json\n{"name": "search", "parameters": {"pattern": "x"}}\n```',
                                      {"search"})
        self.assertEqual(calls[0].arguments, {"pattern": "x"})
        self.assertEqual(parse_text_tool_calls('{"name": "rm_rf", "arguments": {}}', {"search"}), [])
        self.assertEqual(parse_text_tool_calls("no json here"), [])


class Batch(unittest.TestCase):
    def test_batch_dir_and_zip_snapshots_and_resume(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        comp = tmp / "comp"
        (comp / "snapshots" / "t1").mkdir(parents=True)
        (comp / "snapshots" / "t1" / "calc.py").write_text(BUGGY)
        with zipfile.ZipFile(comp / "snapshots" / "t2.zip", "w") as z:
            z.writestr("t2/calc.py", BUGGY)
        (comp / "tasks.jsonl").write_text(
            json.dumps({"instance_id": "t1", "problem_statement": "add subtracts"}) + "\n" +
            json.dumps({"instance_id": "t2", "problem_statement": "add subtracts"}) + "\n")

        def respond(messages):
            n = sum(1 for m in messages if m["role"] == "assistant")
            return [[("read_file", {"path": "calc.py"})],
                    [("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})],
                    [("finish", {"summary": "fixed"})]][min(n, 2)]

        out = tmp / "pred.jsonl"
        cfg = AgentConfig(plan_first=False, baseline_checks=False)
        run_batch(ScriptedClient([respond]), comp, out, tmp / "runs", workers=2, config=cfg)
        rows = {r["instance_id"]: r for r in map(json.loads, out.read_text().splitlines())}
        self.assertEqual(set(rows), {"t1", "t2"})
        for r in rows.values():
            self.assertIn("+    return a + b", r["model_patch"])
            self.assertEqual(r["status"], "unverified")  # no tests in these snapshots
        run_batch(ScriptedClient([respond]), comp, out, tmp / "runs", workers=2, config=cfg)
        self.assertEqual(len(out.read_text().splitlines()), 2, "finished tasks are not rerun")


if __name__ == "__main__":
    unittest.main()
