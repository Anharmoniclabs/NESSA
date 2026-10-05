"""agentharness.toml, skill verification contracts, durable dev processes, MCP and telemetry."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agentharness.__main__ import _config, main as cli_main
from agentharness.agent import Agent, AgentConfig
from agentharness.checks import detect_checks
from agentharness.config import load
from agentharness.dev import DevProcessManager
from agentharness.mcp import McpBus, shared_bus
from agentharness.telemetry import spans_from_events
from agentharness.tests.test_harness_layers import ScriptedClient
from agentharness.workspace import Workspace

PY = json.dumps(sys.executable)

FAKE_MCP = r'''
import json, sys
calls = 0
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, result = msg["method"], None
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "Echo text", "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "crash", "description": "Exit the server",
             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "crash":
            sys.exit(3)
        calls += 1
        result = {"content": [{"type": "text", "text": "echo:" + msg["params"]["arguments"]["text"] + f" #{calls}"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "app.py").write_text("x = 1\n")

    def toml(self, text):
        (self.proj / "agentharness.toml").write_text(text)


class ConfigTests(Base):
    def test_parses_sections_and_reports_invalid_entries(self):
        self.toml(f'''
[context]
max_chars = 2000
[verification]
continuous = ["syntax"]
finish = ["syntax", "lint"]
full_every_edits = 0
[checks]
lint = "echo lint ok"
syntax = "rm -rf /"
[dev.web]
argv = [{PY}, "-m", "http.server"]
health_url = "http://127.0.0.1:8000/"
[dev.bad]
argv = "not a list"
[mcp.remote]
transport = "http"
url = "https://example.com/mcp"
[telemetry]
otlp_endpoint = "http://example.com:4318/v1/traces"
[surprise]
''')
        cfg = load(self.proj)
        self.assertEqual(cfg.context_max_chars, 2000)
        self.assertEqual(cfg.finish, ("syntax", "lint"))
        self.assertEqual(cfg.full_every_edits, 0)
        self.assertEqual(cfg.checks, {"lint": "echo lint ok"})
        self.assertEqual(cfg.dev["web"].health_url, "http://127.0.0.1:8000/")
        self.assertNotIn("bad", cfg.dev)
        self.assertIn("remote", cfg.mcp)  # refused at connection time unless allow_remote
        self.assertEqual(cfg.otlp_endpoint, "")
        joined = "\n".join(cfg.errors)
        for fragment in ("checks.syntax", "dev.bad.argv", "otlp_endpoint", "[surprise]"):
            self.assertIn(fragment, joined)

    def test_parse_error_is_reported_not_raised(self):
        self.toml("[checks\n")
        self.assertIn("cannot parse", load(self.proj).errors[0])

    def test_configured_checks_are_registered(self):
        self.toml('[checks]\nlint = "echo ok"\ntests = "echo custom tests"\n')
        checks = detect_checks(self.proj)
        self.assertEqual(checks["lint"], "echo ok")
        self.assertEqual(checks["tests"], "echo custom tests")

    def test_project_file_fills_defaults_but_cli_flags_win(self):
        self.toml('[verification]\nfinish = ["syntax", "lint"]\nfull_every_edits = 0\n[checks]\nlint = "true"\n')
        ws = Workspace.create(self.proj, self.tmp / "w1")
        agent = Agent(ScriptedClient([[]]), ws, config=AgentConfig(require_approval=False))
        self.assertEqual(agent.config.verify, ("syntax", "lint"))
        self.assertEqual(agent.config.full_verify_every_edits, 0)
        ns = type("A", (), dict(verify="syntax", continuous_verify=None, full_verify_every_edits=None,
                                review_every_edits=None, max_context_chars=1000, tool_output_chars=100,
                                profile="default", max_steps=3, no_plan=True, no_shell=False,
                                text_tools=False, no_baseline=True))()
        cfg = _config(ns, require_approval=False)
        agent = Agent(ScriptedClient([[]]), Workspace.create(self.proj, self.tmp / "w2"), config=cfg)
        self.assertEqual(agent.config.verify, ("syntax",))
        self.assertEqual(agent.config.full_verify_every_edits, 0)

    def test_config_command(self):
        self.toml('[checks]\nlint = "true"\n')
        self.assertEqual(cli_main(["config", str(self.proj)]), 0)
        self.toml('[checks]\nsyntax = "x"\n')
        self.assertEqual(cli_main(["config", str(self.proj)]), 1)


class SkillContractTests(Base):
    def run_agent(self, lint_command):
        self.toml(f'[checks]\nlint = "{lint_command}"\n')
        skill = self.proj / ".agent" / "skills" / "lint-clean"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Keep lint clean.")
        (skill / "skill.json").write_text(json.dumps({"checks": ["lint", "visual review"]}))
        ws = Workspace.create(self.proj, self.tmp / "work")
        client = ScriptedClient([
            [("use_skill", {"name": "lint-clean"})],
            [("read_file", {"path": "app.py"})],
            [("replace_in_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"})],
            [("finish", {"summary": "done"})],
        ])
        agent = Agent(client, ws, config=AgentConfig(require_approval=False, plan_first=False, verify=("syntax",),
                                                     explicit=("verify",), finish_retries=0,
                                                     baseline_checks=False))
        return agent, agent.run("change x")

    def test_skill_declared_registered_check_gates_finish(self):
        agent, result = self.run_agent("false")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("lint", agent.final)
        self.assertEqual(agent.skill_context["lint-clean"]["enforced_checks"], ["lint"])

    def test_skill_check_pass_counts_as_real_verification(self):
        agent, result = self.run_agent("true")
        self.assertEqual(result.status, "verified")

    def test_structural_violation_fails_architecture_check(self):
        (self.proj / "ui").mkdir()
        (self.proj / "ui" / "view.py").write_text("x = 1\n")
        (self.proj / "check_arch.py").write_text(
            "import pathlib, sys\n"
            "bad = [p for p in pathlib.Path('ui').rglob('*.py') if 'import server' in p.read_text()]\n"
            "print('violations:', bad); sys.exit(1 if bad else 0)\n")
        self.toml(f'[verification]\nfinish = ["syntax", "architecture"]\n'
                  f'[checks]\narchitecture = {json.dumps(sys.executable + " check_arch.py")}\n')
        ws = Workspace.create(self.proj, self.tmp / "arch")
        client = ScriptedClient([
            [("read_file", {"path": "ui/view.py"})],
            [("write_file", {"path": "ui/view.py", "content": "import server\n"})],
            [("finish", {"summary": "done"})],
        ])
        result = Agent(client, ws, config=AgentConfig(require_approval=False, plan_first=False, finish_retries=0,
                                                      baseline_checks=False)).run("wire ui to server")
        self.assertEqual(result.status, "failed_checks")
        self.assertIn("[architecture] failed", result.checks["final"]["architecture"])


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/ok" else 503
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"status")

    def log_message(self, *args):
        pass


class DevProcessTests(Base):
    def test_preset_start_health_and_reattach_after_restart(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        server = http.server.HTTPServer(("127.0.0.1", 0), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        from agentharness.config import DevPreset
        presets = {"sleeper": DevPreset([sys.executable, "-c", "import time; time.sleep(30)"], ".",
                                        f"http://127.0.0.1:{port}/ok")}
        evidence = self.tmp / "evidence"
        first = DevProcessManager(ws, evidence, presets)
        self.assertIn("running", first.start("sleeper"))
        self.assertIn("healthy HTTP 200", first.health("sleeper"))
        self.assertIn("unhealthy HTTP 503", first.health("sleeper", f"http://127.0.0.1:{port}/bad"))
        self.assertIn("local http", first.health("sleeper", "http://example.com/"))
        pid = first.processes["sleeper"].pid

        second = DevProcessManager(ws, evidence, presets)  # e.g. after the parent restarted
        self.assertEqual(second.reattached, ["sleeper"])
        self.assertIn("running", second.status("sleeper"))
        self.assertIn("reattached", second.status("sleeper"))
        self.assertIn("stopped", second.stop("sleeper"))
        first.processes["sleeper"].proc.wait(timeout=5)
        self.assertNotIn("running", DevProcessManager(ws, evidence).status("sleeper"))
        self.assertEqual(DevProcessManager(ws, evidence).reattached, [])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_reused_pid_is_not_adopted(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        evidence = self.tmp / "evidence"
        (evidence / "dev").mkdir(parents=True)
        (evidence / "dev" / "processes.json").write_text(json.dumps([{
            "name": "ghost", "argv": ["x"], "cwd": ".", "pid": os.getpid(), "start_ticks": 1,
            "log_path": str(evidence / "dev/ghost.log"), "started_at": 0, "returncode": None}]))
        manager = DevProcessManager(ws, evidence)
        self.assertEqual(manager.reattached, [])
        self.assertIn("exited(unknown)", manager.status("ghost"))
        self.assertIn("already exited", manager.stop("ghost"))  # never signals an unrelated process

    def test_missing_argv_without_preset_is_explained(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        with self.assertRaisesRegex(ValueError, "configured dev processes: none"):
            DevProcessManager(ws, self.tmp / "evidence").start("app")


class McpStdioTests(Base):
    def setUp(self):
        super().setUp()
        (self.proj / "fake_mcp.py").write_text(FAKE_MCP)
        self.toml(f'[mcp.fake]\ntransport = "stdio"\nargv = [{PY}, "fake_mcp.py"]\ntimeout = 10\n')

    def test_discovery_kinds_and_call(self):
        bus = McpBus(load(self.proj).mcp, self.proj, self.tmp / "logs")
        self.addCleanup(bus.close)
        tools = bus.tools()
        self.assertEqual(tools["mcp__fake__echo"].kind, "read")
        self.assertEqual(tools["mcp__fake__crash"].kind, "mcp")
        self.assertEqual(tools["mcp__fake__echo"].schema()["function"]["parameters"]["required"], ["text"])
        self.assertEqual(bus.call("mcp__fake__echo", {"text": "hi"}), "echo:hi #1")

    def test_disconnect_is_an_observation_and_reconnects_next_call(self):
        bus = McpBus(load(self.proj).mcp, self.proj, self.tmp / "logs")
        self.addCleanup(bus.close)
        self.assertTrue(bus.call("mcp__fake__crash", {}).startswith("ERROR: MCP server 'fake'"))
        self.assertEqual(bus.call("mcp__fake__echo", {"text": "back"}), "echo:back #1")  # fresh process

    def test_agent_uses_mcp_tool_in_parent_loop(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        client = ScriptedClient([
            [("mcp__fake__crash", {})],
            [("mcp__fake__echo", {"text": "still here"})],
            [("finish", {"summary": "no change"})],
        ])
        agent = Agent(client, ws, config=AgentConfig(require_approval=False, plan_first=False,
                                                     baseline_checks=False))
        self.addCleanup(agent.mcp.close)
        result = agent.run("use the external tool")
        self.assertEqual(result.status, "no_change")
        tool_results = [m["content"] for m in agent.messages if m["role"] == "tool"]
        self.assertTrue(any(r.startswith("ERROR: MCP server") for r in tool_results))
        self.assertIn("echo:still here #1", tool_results)
        self.assertIs(shared_bus(agent.project_config.mcp, ws.repo, self.tmp / "x"), agent.mcp)

    def test_unavailable_server_is_reported_without_failing_the_agent(self):
        self.toml('[mcp.missing]\ntransport = "stdio"\nargv = ["/nonexistent/mcp-server"]\n')
        ws = Workspace.create(self.proj, self.tmp / "work")
        agent = Agent(ScriptedClient([[("finish", {"summary": "ok"})]]), ws,
                      config=AgentConfig(require_approval=False, plan_first=False, baseline_checks=False))
        self.assertIn("missing", agent.mcp.errors)
        self.assertIn("unavailable", agent._intro("task"))
        self.assertEqual(agent.run("task").status, "no_change")


class McpHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "http"}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": "add", "inputSchema": {"type": "object", "properties": {
                "a": {"type": "number"}, "b": {"type": "number"}}}}]}
        else:
            assert self.headers["Mcp-Session-Id"] == "s1"
            args = msg["params"]["arguments"]
            result = {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
        body = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "s1")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class McpHttpTests(Base):
    def test_streamable_http_with_sse_and_session(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), McpHttpHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.toml(f'[mcp.calc]\ntransport = "http"\nurl = "http://127.0.0.1:{server.server_address[1]}/mcp"\n')
        bus = McpBus(load(self.proj).mcp, self.proj, self.tmp / "logs")
        self.assertEqual(bus.call("mcp__calc__add", {"a": 2, "b": 3}), "5")

    def test_remote_url_requires_opt_in(self):
        self.toml('[mcp.far]\ntransport = "http"\nurl = "https://example.com/mcp"\n')
        bus = McpBus(load(self.proj).mcp, self.proj, self.tmp / "logs")
        self.assertIn("allow_remote", bus.errors["far"])


class TelemetryTests(Base):
    def test_spans_from_events(self):
        events = [dict(event="start", t=10.0), dict(event="model", t=12.0, seconds=2.0, calls=[{}]),
                  dict(event="operation_started", t=12.1, operation_id="o", name="read_file"),
                  dict(event="operation_finished", t=12.3, operation_id="o", status="completed"),
                  dict(event="check", t=13.0, seconds=0.5, name="tests", phase="verify", status="failed")]
        spans = spans_from_events(events)
        self.assertEqual([s["kind"] for s in spans], ["model", "tool", "check"])
        self.assertEqual(spans[2]["status"], "error")

    def test_run_writes_trace_and_timing(self):
        ws = Workspace.create(self.proj, self.tmp / "work")
        client = ScriptedClient([[("read_file", {"path": "app.py"})], [("finish", {"summary": "nothing"})]])
        result = Agent(client, ws, config=AgentConfig(require_approval=False, plan_first=False,
                                                      baseline_checks=False)).run("look")
        evidence = Path(result.evidence_dir)
        timing = json.loads((evidence / "timing.json").read_text())
        self.assertEqual(timing["totals"]["tool"]["count"], 1)
        self.assertEqual(timing["totals"]["model"]["count"], 2)
        spans = json.loads((evidence / "trace.json").read_text())["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(spans[0]["name"], "nessa run")
        self.assertTrue(all(s["parentSpanId"] == spans[0]["spanId"] for s in spans[1:]))

    def test_otlp_export_to_local_collector(self):
        received = []

        class Collector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.toml(f'[telemetry]\notlp_endpoint = "http://127.0.0.1:{server.server_address[1]}/v1/traces"\n')
        ws = Workspace.create(self.proj, self.tmp / "work")
        result = Agent(ScriptedClient([[("finish", {"summary": "x"})]]), ws,
                       config=AgentConfig(require_approval=False, plan_first=False, baseline_checks=False)).run("t")
        self.assertEqual(len(received), 1)
        self.assertIn("HTTP 200", json.loads((Path(result.evidence_dir) / "timing.json").read_text())["export"])


if __name__ == "__main__":
    unittest.main()
