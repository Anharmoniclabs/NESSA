"""Native recurrence: capability gating, adapter payloads, evidence, resume. Fake HTTP servers only."""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentharness import recurrence as rec
from agentharness.__main__ import main
from agentharness.llm import ChatClient, ModelError
from agentharness.telemetry import export_run
from agentharness.tests.test_http_cli import FakeServer

CAP = rec.BackendCapability(True, (1, 2, 4), reports_actual_usage=True, backend_id="fake-backend")
OPTIONS = {"request_field": "recurrence.steps", "usage_field": "usage.recurrence_steps"}
MSG = [{"role": "user", "content": "hi"}]


def config(steps=2, cap=CAP, options=OPTIONS, enabled=True):
    return rec.RecurrenceConfig(enabled, steps, "field-mapping", dict(options), cap)


class Backend:
    """Chat server; `actual` is the recurrence count it reports (None: reports nothing)."""

    def __init__(self, actual=None, stream=False, delay=0.0):
        self.requests, self.actual, self.delay = [], actual, delay
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                time.sleep(outer.delay)
                usage = {"prompt_tokens": 7}
                if outer.actual is not None:
                    usage["recurrence_steps"] = outer.actual
                if body.get("stream"):
                    chunks = [{"choices": [{"index": 0, "delta": {"content": "he"}}]},
                              {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
                              {"choices": [], "usage": usage}]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for c in chunks:
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                data = json.dumps({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                                   "usage": usage}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def backend(self, **kw):
        b = Backend(**kw)
        self.addCleanup(b.close)
        return b


class Validation(unittest.TestCase):
    def test_default_capability_is_unsupported(self):
        self.assertFalse(rec.BackendCapability().native_recurrence)
        self.assertFalse(rec.BackendCapability().supports(1))

    def test_missing_capability_declaration_rejected_before_network(self):
        with self.assertRaisesRegex(rec.RecurrenceError, "capability declaration"):
            ChatClient("http://127.0.0.1:1/v1", "recurrent-looking-model-r4",
                       recurrence=config(cap=rec.BackendCapability()))

    def test_model_name_never_enables_recurrence(self):
        client = ChatClient("http://127.0.0.1:1/v1", "huginn-recurrent-depth")
        self.assertIsNone(client.recurrence)

    def test_invalid_steps(self):
        for bad in (True, False, 0, -1, 1.5, "2", 3, 8):
            with self.subTest(steps=bad), self.assertRaises(rec.RecurrenceError):
                ChatClient("http://127.0.0.1:1/v1", "m", recurrence=config(steps=bad))

    def test_enabled_without_steps_rejected(self):
        with self.assertRaises(rec.RecurrenceError):
            config(steps=None).build_adapter()

    def test_range_capability(self):
        cap = rec.BackendCapability(True, min_steps=2, max_steps=6)
        for ok, bad in ((2, 1), (6, 7)):
            rec.RecurrenceConfig(True, ok, "field-mapping", {"request_field": "r"}, cap).build_adapter()
            with self.assertRaisesRegex(rec.RecurrenceError, "2..6"):
                rec.RecurrenceConfig(True, bad, "field-mapping", {"request_field": "r"}, cap).build_adapter()

    def test_bad_capability_and_adapter_declarations(self):
        for data in ({"native_recurrence": "yes"}, {"native_recurrence": True},
                     {"native_recurrence": True, "steps": [True]}, {"native_recurrence": True, "steps": [1], "min_steps": 1, "max_steps": 2},
                     {"native_recurrence": False, "steps": [1]}, {"bogus": 1}):
            with self.subTest(data=data), self.assertRaises(rec.RecurrenceError):
                rec.BackendCapability.from_dict(data)
        with self.assertRaisesRegex(rec.RecurrenceError, "unknown recurrence adapter"):
            rec.RecurrenceConfig(True, 2, "openai", {}, CAP).build_adapter()
        with self.assertRaisesRegex(rec.RecurrenceError, "collides"):
            config(options={"request_field": "messages"}).build_adapter()
        with self.assertRaisesRegex(rec.RecurrenceError, "reports_actual_usage"):
            config(options={"request_field": "r", "usage_field": "u"},
                   cap=rec.BackendCapability(True, (2,))).build_adapter()
        with self.assertRaisesRegex(rec.RecurrenceError, "cannot read it"):
            config(options={"request_field": "r"}).build_adapter()

    def test_round_trip_and_file(self):
        cfg = config()
        self.assertEqual(rec.RecurrenceConfig.from_dict(cfg.to_dict()), cfg)
        self.assertNotIn("key", json.dumps(cfg.to_dict()).lower())
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "r.toml"
        path.write_text(CONFIG_TOML)
        loaded = rec.load_file(path)
        self.assertFalse(loaded.enabled)
        self.assertEqual(rec.from_cli(4, path).steps, 4)
        self.assertIsNone(rec.from_cli(None, None))
        with self.assertRaisesRegex(rec.RecurrenceError, "needs --recurrence-config"):
            rec.from_cli(2, None)
        with self.assertRaises(rec.RecurrenceError):
            rec.from_cli(3, path)           # not a declared depth
        path.write_text("[recurrence]\nstepz = 1\n")
        with self.assertRaisesRegex(rec.RecurrenceError, "unknown"):
            rec.load_file(path)


CONFIG_TOML = """
[recurrence]
adapter = "field-mapping"
[recurrence.options]
request_field = "recurrence.steps"
usage_field = "usage.recurrence_steps"
[recurrence.capability]
native_recurrence = true
steps = [1, 2, 4]
reports_actual_usage = true
backend_id = "fake-backend"
"""


class Transport(Base):
    def test_unsupported_request_sends_nothing(self):
        b = self.backend()
        with self.assertRaises(rec.RecurrenceError):
            ChatClient(b.url, "m", recurrence=config(steps=3))
        self.assertEqual(b.requests, [])

    def test_exact_payload_and_single_request(self):
        b = self.backend(actual=2)
        reply = ChatClient(b.url, "m", recurrence=config(2)).chat(MSG)
        self.assertEqual(len(b.requests), 1, "one logical inference is one HTTP request")
        self.assertEqual(b.requests[0], {"model": "m", "messages": MSG, "temperature": 0.0, "max_tokens": 4096,
                                         "recurrence": {"steps": 2}})
        self.assertEqual(reply.content, "hello")

    def test_disabled_sends_no_recurrence_fields(self):
        b = self.backend()
        for client in (ChatClient(b.url, "m"), ChatClient(b.url, "m", recurrence=config(2, enabled=False))):
            self.assertIsNone(client.chat(MSG).recurrence)
        for body in b.requests:
            self.assertEqual(set(body), {"model", "messages", "temperature", "max_tokens"})

    def test_actual_steps_explicit_and_unknown(self):
        reply = ChatClient(self.backend(actual=3).url, "m", recurrence=config(4)).chat(MSG)
        self.assertEqual((reply.recurrence["requested_steps"], reply.recurrence["actual_steps"]), (4, 3))
        self.assertEqual((reply.recurrence["backend"], reply.recurrence["model"]), ("fake-backend", "m"))
        self.assertGreaterEqual(reply.recurrence["latency_s"], 0)
        unknown = ChatClient(self.backend().url, "m", recurrence=config(4)).chat(MSG)
        self.assertIsNone(unknown.recurrence["actual_steps"])
        no_report = rec.BackendCapability(True, (4,), reports_actual_usage=False)
        silent = ChatClient(self.backend(actual=4).url, "m",
                            recurrence=config(4, no_report, {"request_field": "recurrence.steps"})).chat(MSG)
        self.assertIsNone(silent.recurrence["actual_steps"], "unreported capability: never trust the value")
        for junk in (True, -1, "4"):
            self.assertIsNone(rec.actual_steps(rec.FieldMappingAdapter("r", "usage.x"), CAP,
                                               {"usage": {"x": junk}}))

    def test_streaming(self):
        b = self.backend(actual=2)
        seen = []
        reply = ChatClient(b.url, "m", on_delta=seen.append, recurrence=config(2)).chat(MSG)
        self.assertEqual("".join(seen), "hello")
        self.assertEqual(len(b.requests), 1)
        self.assertEqual(b.requests[0]["recurrence"], {"steps": 2})
        self.assertTrue(b.requests[0]["stream"])
        self.assertEqual(reply.recurrence["actual_steps"], 2)

    def test_cancellation_keeps_partial_text_and_makes_no_extra_call(self):
        b = self.backend()

        def stop(_):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt) as ctx:
            ChatClient(b.url, "m", on_delta=stop, recurrence=config(2)).chat(MSG)
        self.assertEqual(len(b.requests), 1)
        self.assertTrue(hasattr(ctx.exception, "partial_content"))

    def test_timeout_is_one_request_and_clear_error(self):
        b = self.backend(delay=1.5)
        with self.assertRaises(ModelError):
            ChatClient(b.url, "m", timeout=0.3, retries=1, recurrence=config(2)).chat(MSG)
        self.assertEqual(len(b.requests), 1)

    def test_collision_with_body_field_rejected(self):
        class Bad(rec.RecurrenceAdapter):
            def request_fields(self, steps):
                return {"messages": steps}
        rec.register_adapter("bad", lambda options: Bad())
        self.addCleanup(rec.ADAPTERS.pop, "bad")
        b = self.backend()
        client = ChatClient(b.url, "m", recurrence=rec.RecurrenceConfig(True, 2, "bad", {}, rec.BackendCapability(True, (2,))))
        with self.assertRaises(rec.RecurrenceError):
            client.chat(MSG)
        self.assertEqual(b.requests, [])


class CliAndResume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        self.cfg = self.tmp / "rec.toml"
        self.cfg.write_text(CONFIG_TOML)
        self.server = FakeServer("native")
        self.addCleanup(self.server.close)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def base(self):
        return ["--base-url", self.server.url, "--model", "fake-model"]

    def test_cli_rejects_before_inference_or_workspace(self):
        work = self.tmp / "work"
        code, _, err = self.cli("run", str(self.proj), "t", "--auto-approve", "--work", str(work),
                                *self.base(), "--recurrence-steps", "2")
        self.assertEqual(code, 2)
        self.assertIn("--recurrence-config", err)
        code, _, err = self.cli("run", str(self.proj), "t", "--auto-approve", "--work", str(work),
                                *self.base(), "--recurrence-steps", "3", "--recurrence-config", str(self.cfg))
        self.assertEqual(code, 2)
        self.assertIn("1, 2, 4", err)
        self.assertEqual((self.server.requests, work.exists()), ([], False))

    def test_run_records_evidence_and_resume_keeps_settings(self):
        work = self.tmp / "work"
        code, out, _ = self.cli("run", str(self.proj), "add() subtracts", "--auto-approve", "--work", str(work),
                                *self.base(), "--recurrence-steps", "2", "--recurrence-config", str(self.cfg))
        self.assertEqual(code, 0, out)
        self.assertTrue(all(r["recurrence"] == {"steps": 2} for r in self.server.requests))
        events = [json.loads(l) for l in (work / "evidence" / "events.jsonl").read_text().splitlines()]
        models = [e for e in events if e["event"] == "model"]
        self.assertTrue(models)
        for e in models:
            self.assertEqual((e["recurrence"]["requested_steps"], e["recurrence"]["actual_steps"]), (2, None))
        saved = json.loads((work / "evidence" / "session.json").read_text())
        self.assertEqual(saved["recurrence"]["steps"], 2)
        self.assertNotIn("hidden", json.dumps(models))
        summary = export_run(work / "evidence", "verified", 1.0)
        self.assertEqual(summary["agent_passes"], len(models))
        self.assertEqual(summary["native_recurrence"]["requested_steps_total"], 2 * len(models))
        self.assertIsNone(summary["native_recurrence"]["actual_steps_total"])
        self.assertEqual(summary["native_recurrence"]["actual_unknown_calls"], len(models))

        before = len(self.server.requests)
        code, out, _ = self.cli("resume", str(work), "--extra-steps", "5", *self.base())
        self.assertEqual(code, 0, out)
        self.assertTrue(all(r["recurrence"] == {"steps": 2} for r in self.server.requests[before:]),
                        "settings restored from the session")
        self.assertEqual(json.loads((work / "evidence" / "session.json").read_text())["recurrence"]["steps"], 2)
        n = len(self.server.requests)
        code, _, err = self.cli("resume", str(work), *self.base(), "--recurrence-steps", "4",
                                "--recurrence-config", str(self.cfg))
        self.assertEqual(code, 2)
        self.assertIn("differ from the saved session", err)
        self.assertEqual(len(self.server.requests), n)

    def test_resume_cannot_enable_recurrence_on_plain_session(self):
        work = self.tmp / "work"
        self.assertEqual(self.cli("run", str(self.proj), "add() subtracts", "--auto-approve", "--work", str(work),
                                  *self.base())[0], 0)
        self.assertNotIn("recurrence", self.server.requests[0])
        code, _, err = self.cli("resume", str(work), *self.base(), "--recurrence-steps", "2",
                                "--recurrence-config", str(self.cfg))
        self.assertEqual(code, 2)
        self.assertIn("differ from the saved session", err)


class Evaluation(unittest.TestCase):
    DATA = {"evaluation": {"depths": [4, 1], "budget": {"agent_max_steps": 10}}}

    def test_matched_budgets_and_validated_depths(self):
        arms = rec.plan_arms(self.DATA, config())
        self.assertEqual([a["name"] for a in arms], ["baseline", "native-1", "native-4"])
        self.assertEqual(len({(a["agent_max_steps"], a["time_budget"], a["max_tokens"]) for a in arms}), 1)
        self.assertIsNone(arms[0]["recurrence_steps"])
        with self.assertRaises(rec.RecurrenceError):
            rec.plan_arms({"evaluation": {"depths": [3]}}, config())
        with self.assertRaises(rec.RecurrenceError):
            rec.plan_arms({"evaluation": {"depths": []}}, config())

    def test_summary(self):
        rows = [dict(status="verified", truth=True, seconds=2, prompt_tokens=10, recurrence_requested=2, recurrence_actual=2),
                dict(status="verified", truth=False, seconds=4, recurrence_requested=2, recurrence_actual=None),
                dict(status="failed", truth=False, seconds=6)]
        s = rec.summarize_arm(rows)
        self.assertEqual((s["verified_completion"], s["false_success"], s["latency_mean_s"]), (1, 1, 4))
        self.assertEqual((s["recurrence_requested_total"], s["recurrence_actual_total"],
                          s["recurrence_actual_unknown"], s["prompt_tokens"]), (4, 2, 1, 10))
        self.assertIsNone(rec.summarize_arm([dict(status="failed", truth=False)])["recurrence_actual_total"])

    def test_example_file_plans(self):
        example = Path(__file__).resolve().parents[2] / "configs" / "recurrence-eval.example.toml"
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["recurrence-plan", str(example)])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 4)


@unittest.skipUnless(os.environ.get("NESSA_RECURRENCE_SMOKE_URL"),
                     "opt-in real-backend smoke test: set NESSA_RECURRENCE_SMOKE_URL, NESSA_RECURRENCE_SMOKE_MODEL "
                     "and NESSA_RECURRENCE_SMOKE_CONFIG (documented recurrence TOML) and NESSA_RECURRENCE_SMOKE_STEPS")
class RealBackendSmoke(unittest.TestCase):
    def test_real_transport(self):
        steps = int(os.environ.get("NESSA_RECURRENCE_SMOKE_STEPS", "0")) or None
        cfg = rec.from_cli(steps, os.environ["NESSA_RECURRENCE_SMOKE_CONFIG"])
        client = ChatClient(os.environ["NESSA_RECURRENCE_SMOKE_URL"], os.environ["NESSA_RECURRENCE_SMOKE_MODEL"],
                            max_tokens=32, recurrence=cfg,
                            allow_remote=bool(os.environ.get("NESSA_RECURRENCE_SMOKE_ALLOW_REMOTE")))
        reply = client.chat([{"role": "user", "content": "Reply with the single word: ok"}])
        self.assertTrue(reply.content.strip())
        self.assertEqual(reply.recurrence["requested_steps"], steps)


if __name__ == "__main__":
    unittest.main()
