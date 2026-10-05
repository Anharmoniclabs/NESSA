import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.context import ProjectInstructions
from agentharness.dev import DevProcessManager
from agentharness.llm import Reply, ToolCall
from agentharness.reviewer import Reviewer
from agentharness.skills import SkillRegistry
from agentharness.workspace import Workspace


class ScriptedClient:
    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, messages, tools=None, tool_names=None):
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        raw = [{"id": f"c{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(step)]
        return Reply("", [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(step)],
                     True, raw, prompt_tokens=100)


class HarnessLayers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "app.py").write_text("x = 1\n")

    def test_project_instruction_chain(self):
        (self.proj / "AGENTS.md").write_text("root rule")
        nested = self.proj / "pkg"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("nested rule")
        (nested / "mod.py").write_text("pass\n")
        ctx = ProjectInstructions(self.proj)
        self.assertIn("root rule", ctx.context_for("pkg/mod.py"))
        self.assertIn("nested rule", ctx.context_for("pkg/mod.py"))

    def test_repository_skill_overrides_builtin_namespace(self):
        d = self.proj / ".agent" / "skills" / "custom"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("Do the custom workflow.")
        (d / "skill.json").write_text(json.dumps({
            "description": "custom repo workflow",
            "tools": ["read_file", "run_check"],
            "checks": ["tests"],
        }))
        reg = SkillRegistry(self.proj)
        self.assertIn("custom", reg.names())
        rendered = reg.get("custom").render()
        self.assertIn("custom repo workflow", rendered)
        self.assertIn("run_check", rendered)

    def test_managed_dev_process_captures_logs(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        dev = DevProcessManager(ws, self.tmp / "evidence")
        result = dev.start("hello", [sys.executable, "-u", "-c",
                                    "import time; print('ready'); time.sleep(5)"])
        self.assertIn("pid=", result)
        time.sleep(0.1)
        self.assertIn("ready", dev.logs("hello"))
        self.assertIn("hello:", dev.status("hello"))
        self.assertIn("stopped", dev.stop("hello"))

    def test_edit_creates_checkpoint_and_continuous_check(self):
        ws = Workspace.create(self.proj, self.tmp / "run")
        checks = CheckRunner({"syntax": syntax_check})
        client = ScriptedClient([
            [("read_file", {"path": "app.py"})],
            [("replace_in_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"})],
            [("finish", {"summary": "changed x"})],
        ])
        agent = Agent(client, ws, config=AgentConfig(
            require_approval=False, plan_first=False, baseline_checks=False,
            verify=("syntax",), full_verify_every_edits=0,
        ), checks=checks)
        result = agent.run("change x")
        self.assertEqual(result.status, "unverified")
        checkpoints = list((Path(result.evidence_dir) / "checkpoints").glob("*.diff"))
        self.assertEqual(len(checkpoints), 1)
        events = [json.loads(x) for x in
                  (Path(result.evidence_dir) / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "checkpoint" for e in events))
        self.assertTrue(any(e["event"] == "check" and e.get("phase") == "continuous" for e in events))

    def test_task_acceptance_is_separate_and_skipped_grader_is_not_a_pass(self):
        ws = Workspace.create(self.proj, self.tmp / "acceptance")
        grader = lambda ws, task, summary: {
            "status": "skipped", "scope": "CSV source unchanged", "evidence": "fixture missing"}
        client = ScriptedClient([[("finish", {"summary": "I did not validate the CSV."})]])
        result = Agent(client, ws, config=AgentConfig(
            require_approval=False, plan_first=False, baseline_checks=False, finish_retries=0),
            checks=CheckRunner({"syntax": syntax_check}), acceptance_grader=grader).run("Check CSV")

        self.assertEqual(result.status, "failed_checks")
        self.assertEqual(result.checks["final"], {})
        self.assertEqual(result.acceptance["status"], "skipped")
        saved = json.loads((Path(result.evidence_dir) / "result.json").read_text())
        self.assertEqual(saved["acceptance"]["scope"], "CSV source unchanged")

    def test_task_acceptance_pass_does_not_relabel_existing_check_scope(self):
        ws = Workspace.create(self.proj, self.tmp / "acceptance")
        client = ScriptedClient([[("finish", {"summary": "The source is unchanged."})]])
        result = Agent(client, ws, config=AgentConfig(
            require_approval=False, plan_first=False, baseline_checks=False),
            checks=CheckRunner({"syntax": syntax_check}),
            acceptance_grader=lambda ws, task, summary: {
                "status": "passed", "scope": "CSV input unchanged", "evidence": "sha256 verified"}
        ).run("Check CSV")

        self.assertEqual(result.status, "no_change")
        self.assertEqual(result.checks["final"], {})
        self.assertEqual(result.acceptance["status"], "passed")

    def test_reviewer_is_advisory_text_only(self):
        class ReviewClient:
            model = "reviewer"
            def chat(self, messages, tools=None, tool_names=None):
                self.messages = messages
                self.tools = tools
                return Reply("Check the empty-input edge case.", [], False)
        c = ReviewClient()
        notes = Reviewer(c).review("fix parser", "diff --git a/x b/x\n")
        self.assertIn("empty-input", notes)
        self.assertIsNone(c.tools)


if __name__ == "__main__":
    unittest.main()
