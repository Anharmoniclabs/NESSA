"""Enterprise-parity behaviour: direct edits, permission modes, git, model completion,
subagents and transcripts."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.permissions import Permissions
from agentharness.subagents import load_definitions
from agentharness.workspace import Workspace

from .test_agent import UNITTEST, ScriptedClient, make_project

FIX = [("read_file", {"path": "calc.py"}),
       ("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a + b"})]


class Recorder:
    """Permission asker that records questions and answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, name, args):
        self.asked.append(name)
        return self.answers.pop(0) if self.answers else (False, "", False)


class Base(unittest.TestCase):
    direct = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = make_project(self.tmp)
        make = Workspace.direct if self.direct else Workspace.create
        self.ws = make(self.proj, self.tmp / "work")

    def agent(self, steps, asker=None, sub_steps=None, **cfg):
        cfg.setdefault("plan_first", False)
        cfg.setdefault("permission_mode", "bypass")
        checks = CheckRunner({"syntax": syntax_check, "tests": UNITTEST}, timeout=60)
        self.client = ScriptedClient(steps)
        self.sub = ScriptedClient(sub_steps) if sub_steps else None
        return Agent(self.client, self.ws, config=AgentConfig(require_approval=False, **cfg),
                     checks=checks, asker=asker, subagent_client=self.sub)


class DirectWorkspace(Base):
    def test_edits_change_the_real_project_and_keep_diff_and_undo(self):
        result = self.agent([FIX, [("finish", {"summary": "fixed"})]]).run("fix add")
        self.assertEqual(result.status, "verified")
        self.assertIn("a + b", (self.proj / "calc.py").read_text())
        self.assertIn("-    return a - b", result.patch)
        self.assertEqual(self.ws.apply_to(self.proj), [])  # nothing left to apply
        self.ws.undo("calc.py")
        self.assertIn("a - b", (self.proj / "calc.py").read_text())

    def test_reopen_finds_the_project_for_resume(self):
        again = Workspace.open(self.tmp / "work")
        self.assertTrue(again.direct)
        self.assertEqual(again.repo, self.proj.resolve())

    def test_direct_prompt_does_not_claim_a_private_copy(self):
        agent = self.agent(["hi"])
        self.assertNotIn("private copy", agent._system())


class PermissionModes(Base):
    def test_plan_mode_denies_edits(self):
        result = self.agent([FIX, [("finish", {"summary": "done"})]], permission_mode="plan").run("fix add")
        self.assertIn("a - b", (self.proj / "calc.py").read_text())
        self.assertEqual(result.status, "no_change")

    def test_default_mode_asks_and_respects_denial(self):
        asker = Recorder((False, "not now", False))
        self.agent([FIX, [("finish", {"summary": "done"})]], asker=asker,
                   permission_mode="default").run("fix add")
        self.assertEqual(asker.asked, ["replace_in_file"])  # reads never ask
        self.assertIn("a - b", (self.proj / "calc.py").read_text())
        tool_results = [m["content"] for m in self.client.seen[1][0] if m["role"] == "tool"]
        self.assertTrue(any("denied" in t and "not now" in t for t in tool_results))

    def test_always_is_remembered_per_tool(self):
        permissions = Permissions("default", Recorder((True, "", True)))
        self.assertEqual(permissions.check("write_file", "edit", {}), (True, ""))
        self.assertEqual(permissions.check("write_file", "edit", {}), (True, ""))
        self.assertEqual(permissions.asker.asked, ["write_file"])

    def test_accept_edits_still_asks_for_commands(self):
        permissions = Permissions("accept_edits", Recorder())
        self.assertTrue(permissions.check("replace_in_file", "edit", {})[0])
        self.assertFalse(permissions.check("run_command", "check", {"command": "rm -rf x"})[0])
        self.assertTrue(permissions.check("run_check", "check", {"name": "tests"})[0])

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            Permissions("yolo")


class ModelCompletion(Base):
    def test_model_decides_without_running_verification(self):
        result = self.agent([FIX, [("finish", {"summary": "done"})]], completion="model").run("fix add")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.checks["final"], {})

    def test_failing_finish_hook_sends_the_model_back(self):
        steps = [[("read_file", {"path": "calc.py"}),
                  ("replace_in_file", {"path": "calc.py", "old": "a - b", "new": "a * b"})],
                 [("finish", {"summary": "done"})]]
        result = self.agent(steps, completion="model", finish_hooks=("tests",), finish_retries=1).run("fix")
        self.assertEqual(result.status, "failed_checks")
        prompts = json.dumps(self.client.seen[-1][0])
        self.assertIn("finish hook failed", prompts)


class GitTools(Base):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        run = lambda *a: subprocess.run(["git", *a], cwd=self.proj, check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.proj, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.proj, check=True)

    def test_read_only_git_and_unsafe_options(self):
        agent = self.agent(["x"])
        tool = agent.tools["git"]
        self.assertIn("init", tool.handler(agent, {"command": "log", "args": ["--oneline"]}))
        with self.assertRaisesRegex(Exception, "Not allowed"):
            tool.handler(agent, {"command": "diff", "args": ["--output=/tmp/x"]})

    def test_commit_goes_through_permissions(self):
        asker = Recorder((True, "", False), (True, "", False))
        steps = [FIX, [("git_commit", {"message": "fix add"})], [("finish", {"summary": "done"})]]
        self.agent(steps, asker=asker, permission_mode="default").run("fix and commit")
        self.assertEqual(asker.asked, ["replace_in_file", "git_commit"])
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.proj, capture_output=True, text=True).stdout
        self.assertIn("fix add", log)

    def test_private_workflow_has_no_commit_tool(self):
        agent = self.agent(["x"], permission_mode=None)
        self.assertNotIn("git_commit", agent.tools)


class Subagents(Base):
    def test_explore_subagent_has_own_context_and_returns_report(self):
        parent = [[("agent", {"agent_type": "explore", "prompt": "Where is add defined?"})],
                  [("finish", {"summary": "answered"})]]
        sub = [[("search", {"pattern": "def add"})],
               [("report", {"result": "add is defined in calc.py:1"})]]
        agent = self.agent(parent, sub_steps=sub)
        agent.run("find add")
        first_sub_prompt = json.dumps(self.sub.seen[0][0])
        self.assertIn("Where is add defined?", first_sub_prompt)
        self.assertNotIn("find add", first_sub_prompt)  # fresh context, not the parent's
        self.assertNotIn("agent", self.sub.seen[0][1])  # no nesting
        self.assertNotIn("replace_in_file", self.sub.seen[0][1])  # explore is read-only
        parent_view = json.dumps(self.client.seen[1][0])
        self.assertIn("add is defined in calc.py:1", parent_view)
        rows = agent.transcript.read()
        self.assertTrue(any(r["agent"] == "sub-01-explore" and r["role"] == "tool" for r in rows))
        self.assertTrue(any(r["agent"] == "main" and r["role"] == "system" for r in rows))

    def test_general_subagent_edits_through_parent_permissions(self):
        asker = Recorder((False, "", False))
        parent = [[("agent", {"agent_type": "general", "prompt": "fix add in calc.py"})],
                  [("finish", {"summary": "done"})]]
        agent = self.agent(parent, asker=asker, sub_steps=[FIX, [("report", {"result": "tried"})]],
                           permission_mode="default")
        agent.run("fix add")
        self.assertEqual(asker.asked, ["replace_in_file"])
        self.assertIn("a - b", (self.proj / "calc.py").read_text())

    def test_subagents_are_read_only_during_planning(self):
        parent = [[("agent", {"agent_type": "general", "prompt": "fix add"})],
                  [("respond", {"message": "ok"})]]
        self.agent(parent, sub_steps=[[("report", {"result": "x"})]], plan_first=True).run("fix add")
        self.assertNotIn("replace_in_file", self.sub.seen[0][1])

    def test_unknown_type_and_budget(self):
        agent = self.agent(["x"], subagent_max_runs=0)
        self.assertIn("unknown agent type", agent.run_subagent("nope", "x"))
        self.assertIn("budget", agent.run_subagent("explore", "x"))

    def test_custom_definition_from_project(self):
        folder = self.proj / ".nessa" / "agents"
        folder.mkdir(parents=True)
        (folder / "log.md").write_text("---\nname: log-reader\ndescription: Parse test logs\n"
                                       "model: nessa-spec:latest\ntools: read_file, search\nmax_steps: 4\n---\n"
                                       "Return {error, file, line}.\n")
        found = load_definitions(self.proj, home=self.tmp)
        d = found["log-reader"]
        self.assertEqual((d.model, d.tools, d.max_steps), ("nessa-spec:latest", ("read_file", "search"), 4))
        self.assertIn("{error, file, line}", d.prompt)
        self.assertIn("explore", found)


class PrivateWorkflowUnchanged(Base):
    direct = False

    def test_legacy_mode_has_no_permission_prompts(self):
        asker = Recorder()
        result = self.agent([FIX, [("finish", {"summary": "fixed"})]], asker=asker,
                            permission_mode=None).run("fix add")
        self.assertEqual(result.status, "verified")
        self.assertEqual(asker.asked, [])
        self.assertIn("a - b", (self.proj / "calc.py").read_text())  # original untouched


if __name__ == "__main__":
    unittest.main()


class ChatRouting(Base):
    def test_clear_build_request_skips_the_chat_decision_turn(self):
        agent = self.agent([[("finish", {"summary": "nothing to do"})]], conversational=True, efficient_chat=True)
        agent.run("write a game in python for spades and launch to test")
        first_tools = self.client.seen[0][1]
        self.assertIn("write_file", first_tools)  # the first model call is already in the work phase
        self.assertNotIn("start_work", first_tools)

    def test_ordinary_chat_still_goes_to_the_model(self):
        agent = self.agent(["Here is a short poem."], conversational=True, efficient_chat=True)
        result = agent.run("write me a poem")
        self.assertEqual(result.status, "answered")


class Scaffolding(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "empty").mkdir()
        self.ws = Workspace.direct(self.tmp / "empty", self.tmp / "work")

    def agent(self, steps, **cfg):
        self.client = ScriptedClient(steps)
        config = AgentConfig(require_approval=False, plan_first=False, permission_mode="bypass",
                             completion="model", finish_hooks=("tests",), **cfg)
        return Agent(self.client, self.ws, config=config, checks=CheckRunner({"syntax": syntax_check}))

    def test_empty_folder_gets_the_project_recipe(self):
        self.agent([[("finish", {"summary": "x"})]]).run("build a spades card game")
        intro = json.dumps(self.client.seen[0][0])
        self.assertIn("complete, runnable project", intro)
        self.assertIn("one file per write_file call", intro)

    def test_tests_written_during_the_run_must_pass_before_finishing(self):
        bad_test = ("import unittest\nclass T(unittest.TestCase):\n"
                    "    def test_x(self):\n        self.assertEqual(1, 2)\n")
        steps = [[("write_file", {"path": "game/__init__.py", "content": ""})],
                 [("write_file", {"path": "tests/__init__.py", "content": ""})],
                 [("write_file", {"path": "tests/test_rules.py", "content": bad_test})],
                 [("finish", {"summary": "done"})]]
        result = self.agent(steps, finish_retries=0).run("build a game")
        self.assertEqual(result.status, "failed_checks")  # the new tests were found and enforced

    def test_project_without_tests_can_still_finish(self):
        result = self.agent([[("write_file", {"path": "main.py", "content": "print(1)\n"})],
                             [("finish", {"summary": "done"})]]).run("write a script")
        self.assertEqual(result.status, "completed")

    def test_claimed_success_without_files_is_sent_back(self):
        steps = [[("finish", {"summary": "Built the game. All requirements satisfied."})],
                 [("write_file", {"path": "main.py", "content": "print(1)\n"})],
                 [("finish", {"summary": "done"})]]
        result = self.agent(steps).run("build a local spades card game")
        self.assertEqual(result.status, "completed")
        self.assertIn("no files have changed", json.dumps(self.client.seen[1][0]))

    def test_repeated_false_claims_end_as_no_change_not_success(self):
        result = self.agent([[("finish", {"summary": "Built it."})]], finish_retries=1).run("build a game")
        self.assertEqual(result.status, "no_change")
        self.assertIn("none were made", result.summary)


class BareToolArguments(Base):
    def test_bare_arguments_run_the_only_matching_tool(self):
        steps = ['{\n  "command": "echo hi > out.txt",\n  "timeout": 5\n}', [("finish", {"summary": "ran it"})]]
        result = self.agent(steps).run("run the setup command")
        self.assertTrue((self.proj / "out.txt").exists())
        self.assertNotEqual(result.summary.strip()[:1], "{")

    def test_ambiguous_or_unknown_keys_are_not_guessed(self):
        agent = self.agent(["x"])
        self.assertIsNone(agent._bare_call('{"path": "calc.py"}', ["read_file", "outline", "list_dir"]))
        self.assertIsNone(agent._bare_call('{"bogus": 1}', ["run_command"]))
        call = agent._bare_call('{"command": "ls"}', ["run_command", "read_file"])
        self.assertEqual((call.name, call.arguments), ("run_command", {"command": "ls"}))


class ComputerAccess(Base):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / "Projects" / "cards" / "spades").mkdir(parents=True)
        (self.home / "Projects" / "cards" / "spades" / "game.py").write_text(
            "import pathlib\npathlib.Path('ran.txt').write_text('yes')\n")
        (self.home / "Projects" / ".secret").mkdir()
        (self.home / "Projects" / ".secret" / "spades_key.txt").write_text("x")

    def test_find_by_name_skips_hidden_folders(self):
        agent = self.agent(["x"], local_roots=(str(self.home / "Projects"),))
        out = agent.tools["local_find"].handler(agent, {"name": "spades"})
        self.assertIn("cards/spades/", out)
        self.assertNotIn(".secret", out)
        self.assertIn("No files", agent.tools["local_find"].handler(agent, {"name": "*.zzz"}))

    def test_launch_needs_permission_and_runs_detached(self):
        import sys, time
        asker = Recorder((True, "", False))
        agent = self.agent(["x"], asker=asker, permission_mode="default", local_roots=(str(self.home),))
        game = self.home / "Projects" / "cards" / "spades"
        call = __import__("agentharness.llm", fromlist=["ToolCall"]).ToolCall(
            "c1", "launch_program", {"argv": [sys.executable, "game.py"], "cwd": str(game)})
        out = agent._execute(call, ["launch_program"])
        self.assertEqual(asker.asked, ["launch_program"])
        self.assertIn("exited immediately with code 0", out)
        self.assertEqual((game / "ran.txt").read_text(), "yes")

    def test_launch_outside_the_allowed_folders_is_refused(self):
        agent = self.agent(["x"], local_roots=(str(self.home / "Projects"),))
        with self.assertRaisesRegex(Exception, "outside"):
            agent.tools["launch_program"].handler(agent, {"argv": ["ls"], "cwd": "/etc"})

    def test_chat_can_find_then_launch_with_approval(self):
        asker = Recorder((True, "", False))
        steps = [[("local_find", {"name": "spades"})],
                 [("launch_program", {"argv": ["true"], "cwd": str(self.home / "Projects/cards/spades")})],
                 "Your spades game is running."]
        agent = self.agent(steps, asker=asker, permission_mode="default", conversational=True,
                           efficient_chat=True, local_roots=(str(self.home),))
        result = agent.run("find my spades game and run it")
        self.assertEqual(result.status, "answered")
        self.assertEqual(asker.asked, ["launch_program"])  # finding is free; launching asks
