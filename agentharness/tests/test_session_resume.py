"""Durable session, project configuration and end-to-end resume tests."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.config import ProjectConfig, load_project_config
from agentharness.llm import Reply, ToolCall
from agentharness.session import SessionStore
from agentharness.workspace import Workspace


class ScriptedClient:
    model = "scripted-resume"

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, messages, tools=None, tool_names=None):
        if not self.steps:
            raise AssertionError("script exhausted")
        step = self.steps.pop(0)
        raw = [{"id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
               for i, (name, args) in enumerate(step)]
        calls = [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(step)]
        return Reply("", calls, True, raw, prompt_tokens=100)


class DurableResume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text("value = 1\n")
        self.work = self.tmp / "run"

    def _checks(self):
        return CheckRunner({"syntax": syntax_check})

    def test_resume_continues_same_workspace_and_event_log(self):
        ws = Workspace.create(self.project, self.work)
        store = SessionStore(self.work, self.project)
        first = Agent(
            ScriptedClient([
                [("read_file", {"path": "app.py"})],
                [("replace_in_file", {"path": "app.py", "old": "value = 1", "new": "value = 2"})],
            ]),
            ws,
            config=AgentConfig(
                require_approval=False,
                plan_first=False,
                baseline_checks=False,
                verify=("syntax",),
                max_steps=2,
                full_verify_every_edits=0,
            ),
            checks=self._checks(),
            session=store,
            source_project=self.project,
        )
        stopped = first.run("change value to 3")
        self.assertEqual(stopped.status, "budget_exhausted")
        self.assertEqual((ws.repo / "app.py").read_text(), "value = 2\n")
        self.assertEqual((self.project / "app.py").read_text(), "value = 1\n")
        before_events = [json.loads(x) for x in
                         (self.work / "evidence" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(before_events[0]["event"], "start")

        reopened = Workspace.open(self.work)
        resumed = Agent(
            ScriptedClient([
                [("read_file", {"path": "app.py"})],
                [("replace_in_file", {"path": "app.py", "old": "value = 2", "new": "value = 3"})],
                [("finish", {"summary": "value is now 3"})],
            ]),
            reopened,
            config=AgentConfig(
                require_approval=False,
                plan_first=False,
                baseline_checks=False,
                verify=("syntax",),
                max_steps=3,
                full_verify_every_edits=0,
            ),
            checks=self._checks(),
            session=SessionStore(self.work),
            source_project=self.project,
            resume=True,
        )
        result = resumed.run()
        self.assertEqual(result.status, "unverified")
        self.assertEqual((reopened.repo / "app.py").read_text(), "value = 3\n")
        self.assertEqual((self.project / "app.py").read_text(), "value = 1\n",
                         "resume must still leave the original project untouched")

        events = [json.loads(x) for x in
                  (self.work / "evidence" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["event"] == "start" for e in events), 1)
        self.assertEqual(sum(e["event"] == "resume" for e in events), 1)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

        state = SessionStore(self.work).load()
        self.assertEqual(state["status"], "unverified")
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["resume_count"], 1)
        self.assertEqual(state["edit_count"], 2)
        self.assertEqual(state["task"], "change value to 3")

        digest = json.loads((self.work / "evidence" / "context-digest.json").read_text())
        self.assertEqual(digest["changed_files"], ["app.py"])
        self.assertEqual(digest["edit_count"], 2)

    def test_project_config_loads_checks_verification_and_dev_profiles(self):
        (self.project / "agentharness.toml").write_text(
            "[checks]\n"
            "lint = \"python -m compileall -q .\"\n\n"
            "[verification]\n"
            "continuous = [\"syntax\", \"lint\"]\n"
            "finish = [\"syntax\", \"lint\"]\n"
            "full_every_edits = 2\n\n"
            "[reviewer]\n"
            "every_edits = 3\n\n"
            "[context]\n"
            "max_chars = 9000\n\n"
            "[dev.web]\n"
            "argv = [\"python\", \"-m\", \"http.server\", \"8000\"]\n"
            "cwd = \".\"\n"
        )
        cfg = load_project_config(self.project)
        self.assertEqual(cfg.checks["lint"], "python -m compileall -q .")
        self.assertEqual(cfg.continuous_verify, ("syntax", "lint"))
        self.assertEqual(cfg.finish_verify, ("syntax", "lint"))
        self.assertEqual(cfg.full_verify_every_edits, 2)
        self.assertEqual(cfg.review_every_edits, 3)
        self.assertEqual(cfg.context_max_chars, 9000)
        self.assertEqual(cfg.dev["web"].argv[-1], "8000")
        self.assertIn("web:", cfg.dev_summary())

    def test_session_messages_exist_before_run_finishes(self):
        ws = Workspace.create(self.project, self.work)
        store = SessionStore(self.work, self.project)
        agent = Agent(
            ScriptedClient([[("finish", {"summary": "nothing needed"})]]),
            ws,
            config=AgentConfig(
                require_approval=False,
                plan_first=False,
                baseline_checks=False,
                max_steps=1,
            ),
            checks=self._checks(),
            session=store,
            source_project=self.project,
        )
        result = agent.run("inspect only")
        self.assertEqual(result.status, "no_change")
        self.assertTrue(store.messages())
        self.assertTrue((self.work / "evidence" / "context-digest.json").is_file())


if __name__ == "__main__":
    unittest.main()
