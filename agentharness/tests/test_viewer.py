"""Offline tests: only synthetic filesystem fixtures, never model/API calls."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agentharness.viewer import MAX_FILE, RunStore, SafeRoot, ViewerServer, clean, main


@unittest.skipUnless(os.name == "posix", "secure POSIX reader")
class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runs"
        self.root.mkdir()
        self.store = RunStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def events(self, relative, events):
        return self.write(relative, "\n".join(json.dumps(e) for e in events) + "\n")

    def one(self):
        listing = self.store.listing()
        self.assertEqual(len(listing["runs"]), 1)
        return self.store.detail(listing["runs"][0]["id"])

    def test_empty_root_is_not_demo_success(self):
        self.assertEqual(self.store.listing()["runs"], [])

    def test_three_stage_controller_vs_advisory_and_audit(self):
        self.write("one/job.json", {"task": "Fix parser", "model": "local-model", "environment": {"SECRET": "hidden-env"}})
        self.write("one/work/evidence/result.json", {"status": "no_change", "changed_files": [], "plan": {"goal": "Fix", "files": ["parser.py"], "steps": ["Repair"], "checks": ["tests"]}, "checks": {"final": {"tests": "[tests] failed (exit=1, 0.1s)"}}, "response_stage": {"advisory_text": "Everything passed!", "status": "recorded"}, "seconds": 2.5})
        self.events("one/work/evidence/events.jsonl", [{"event": "plan", "plan": {"goal": "Fix"}}, {"event": "approval", "approved": True}, {"event": "stage", "phase": "solve"}, {"event": "model", "reasoning": "HIDDEN_THINKING", "content": "HIDDEN_CONTENT"}])
        self.write("one/audit.json", {"independent_grade": {"status": "failed", "cases": 30}, "prompt_tokens": 0, "original_unchanged": True, "environment": "hidden-audit", "patch_replay_tests": {"returncode": 1, "output": "unfiltered output"}})
        self.write("one/job-result.json", {"exit_code": 0, "wall_seconds": 3})
        d = self.one()
        self.assertEqual(d["status"], "no_change")
        self.assertTrue(d["approved"])
        self.assertEqual(d["changed_files"], [])
        self.assertEqual(d["checks"]["final"]["tests"]["status"], "failed")
        self.assertEqual(d["independent"]["status"], "failed")
        self.assertFalse(d["advisory"]["authoritative"])
        self.assertEqual(d["metrics"]["prompt_tokens"], 0)
        self.assertIsNone(d["metrics"]["completion_tokens"])
        payload = json.dumps(d)
        for forbidden in ("hidden-env", "HIDDEN_THINKING", "HIDDEN_CONTENT", "hidden-audit", "unfiltered output"):
            self.assertNotIn(forbidden, payload)

    def test_supported_layouts_and_transaction_audit(self):
        for relative in ("legacy/evidence", "nested/batch/code/work/evidence", "direct"):
            self.write(relative + "/result.json", {"status": "verified", "changed_files": ["a.py"], "checks": {"final": {"tests": "[tests] passed (exit=0, 1.0s)"}}})
        self.write("nested/batch/code/multifile-audit.json", {"external_grade": {"status": "failed", "panels": {"integration": {"status": "failed", "assertions": 2, "error": "Contract mismatch"}}}, "fully_verified": False})
        listing = self.store.listing()
        self.assertEqual(len(listing["runs"]), 3)
        tx = next(r for r in listing["runs"] if "nested" in r["name"])
        detail = self.store.detail(tx["id"])
        self.assertEqual(detail["status"], "verified")
        self.assertEqual(detail["independent"]["status"], "failed")
        self.assertFalse(detail["integrity"]["fully_verified"])

    def test_missing_result_stays_in_progress_or_interrupted(self):
        self.events("one/evidence/events.jsonl", [{"event": "stage", "phase": "intake"}])
        d = self.one()
        self.assertEqual(d["status"], "in_progress_or_interrupted")
        self.assertFalse(d["ended"])
        self.assertIsNone(d["changed_files"])
        self.assertIsNone(d["patch"])
        self.assertTrue(all(v is None for v in d["metrics"].values()))

    def test_exit_zero_without_result_is_not_verified(self):
        self.write("job/job-result.json", {"exit_code": 0, "wall_seconds": 1})
        self.assertEqual(self.one()["status"], "unavailable")

    def test_cancelled_job(self):
        self.write("job/cancelled.json", {"status": "not_run", "reason": "Unsupported transport"})
        d = self.one()
        self.assertEqual(d["status"], "not_run")
        self.assertEqual(d["failures"][0]["message"], "Unsupported transport")

    def test_malformed_results_events_and_nested_types(self):
        self.write("one/evidence/result.json", '{"status":')
        self.write("one/job.json", [])
        self.write("one/cancelled.json", {"status": []})
        self.events("one/evidence/events.jsonl", [{"event": []}, {"event": "stage", "phase": []}, {"event": "check", "phase": "verify", "name": "tests", "status": []}, {"event": "model", "reasoning": "never show"}, {"event": "stage", "phase": "solve"}])
        with (self.root / "one/evidence/events.jsonl").open("a") as f:
            f.write('{"event":')
        d = self.one()
        self.assertEqual(d["last_stage"], "solve")
        self.assertEqual(d["checks"]["final"]["tests"]["status"], "unknown")
        self.assertGreaterEqual(len(d["warnings"]), 3)

    def test_oversized_file_never_loaded(self):
        self.write("one/evidence/result.json", "x" * (MAX_FILE + 1))
        d = self.one()
        self.assertEqual(d["status"], "unavailable")
        self.assertIn("exceeds size limit", " ".join(d["warnings"]))

    def test_nonfinite_metrics_not_zero_or_invalid_json(self):
        self.write("one/job-result.json", '{"wall_seconds": NaN}')
        d = self.one()
        self.assertIsNone(d["metrics"]["process_wall_seconds"])
        json.dumps(d, allow_nan=False)

    def test_missing_result_can_use_end_event_without_promoting_model(self):
        self.events("one/evidence/events.jsonl", [{"event": "response", "advisory_text": "verified"}, {"event": "end", "status": "failed_checks", "changed": ["x.py"]}])
        d = self.one()
        self.assertEqual(d["status"], "failed_checks")
        self.assertEqual(d["status_source"], "end event")
        self.assertEqual(d["changed_files"], ["x.py"])

    def test_latest_checkpoint_is_labeled_not_final(self):
        self.events("one/evidence/events.jsonl", [{"event": "checkpoint", "changed": ["a.py"], "edit": 2}])
        self.write("one/evidence/checkpoints/edit-0001.diff", "old")
        self.write("one/evidence/checkpoints/edit-0002.diff", "new")
        self.write("one/evidence/checkpoints/not-evidence.txt", "hidden")
        d = self.one()
        self.assertEqual(d["patch"], "new")
        self.assertIn("not final", d["patch_source"])
        self.assertIn("not final", d["changed_source"])
        self.assertNotIn("not-evidence", json.dumps(d))

    def test_symlinks_hardlinks_special_files_and_traversal_are_rejected(self):
        external = Path(self.temp.name) / "outside.json"
        external.write_text('{"status":"verified","summary":"SECRET_OUTSIDE"}')
        self.write("one/job.json", {})
        ev = self.root / "one/evidence"
        ev.mkdir()
        (ev / "result.json").symlink_to(external)
        self.assertIsNone(self.store.root.read("one/evidence/result.json")[0])
        os.link(external, ev / "patch.diff")
        self.assertIsNone(self.store.root.read("one/evidence/patch.diff")[0])
        os.mkfifo(ev / "events.jsonl")
        self.assertIsNone(self.store.root.read("one/evidence/events.jsonl")[0])
        for path in ("../outside.json", "/etc/passwd", "one/../outside.json", "one\\evidence/result.json"):
            self.assertIsNone(self.store.root.read(path)[0])
        (self.root / "linked-run").symlink_to(ev, target_is_directory=True)
        (self.root / "linked-root").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            SafeRoot(self.root / "linked-root")
        payload = json.dumps(self.one())
        self.assertNotIn("SECRET_OUTSIDE", payload)

    def test_pinned_root_survives_directory_replacement(self):
        self.write("one/evidence/result.json", {"status": "no_change"})
        listing = self.store.listing()
        old = self.root / "one"
        old.rename(self.root / "old-one")
        outside = Path(self.temp.name) / "external"
        outside.mkdir()
        (old).symlink_to(outside, target_is_directory=True)
        d = self.store.detail(listing["runs"][0]["id"])
        self.assertEqual(d["status"], "unavailable")

    def test_private_channels_credentials_and_sensitive_patches(self):
        self.write("one/evidence/result.json", {"status": "no_change", "response_stage": {"advisory_text": "<think>PRIVATE_REASON</think>Done", "status": "recorded"}, "patch": 'diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n+CUSTOM=SECRET_VALUE\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+password = "secret-password"\n+safe = True\n'})
        d = self.one()
        payload = json.dumps(d)
        for value in ("PRIVATE_REASON", "SECRET_VALUE", "secret-password"):
            self.assertNotIn(value, payload)
        self.assertIn("safe = True", d["patch"])
        self.assertIn("redacted", d["patch"])
        self.assertIn("omitted", d["advisory"]["text"])
        self.assertNotIn("pass123", clean("https://user:pass123@example.com"))

    def test_quoted_sensitive_patch_paths_are_omitted(self):
        self.write("one/evidence/result.json", {"status": "no_change", "patch": 'diff --git "a/.env" "b/.env"\n--- "a/.env"\n+++ "b/.env"\n+CUSTOM=QUOTED_PATH_CANARY_123\n'})
        self.assertNotIn("QUOTED_PATH_CANARY", json.dumps(self.one()))

    def test_credential_assignment_variants_are_redacted(self):
        cases = [
            "OPENAI_API_KEY=ENV_CANARY_0123456789",
            '{"api_key": "JSON_CANARY_0123456789"}',
            "os.environ['API_KEY'] = 'PY_CANARY_0123456789'",
            "AWS_SECRET_ACCESS_KEY=AWS_CANARY_0123456789",
            '"AWS_ACCESS_KEY_ID": "ACCESS_CANARY_0123456789"',
        ]
        for text in cases:
            self.assertNotIn("CANARY", clean(text), text)
            self.assertIn("redacted", clean(text), text)

    def test_huge_metrics_are_rejected_without_disconnect(self):
        self.write("one/evidence/result.json", {"status": "no_change", "seconds": 10 ** 400})
        d = self.one()
        self.assertIsNone(d["metrics"]["run_seconds"])
        self.assertEqual(d["status"], "no_change")

    def test_ignored_workspaces_do_not_become_runs(self):
        self.write("repo/evidence/result.json", {"status": "verified"})
        self.write("models/evidence/result.json", {"status": "verified"})
        self.write(".hidden/evidence/result.json", {"status": "verified"})
        self.assertEqual(self.store.listing()["runs"], [])

    def test_refresh_discovers_new_runs_and_deleted_run_reports_absence(self):
        self.assertEqual(self.store.listing()["runs"], [])
        self.write("new/evidence/result.json", {"status": "no_change"})
        self.store.updated = 0
        self.assertEqual(len(self.store.listing()["runs"]), 1)
        with self.assertRaises(KeyError):
            self.store.detail("0" * 24)

    def test_discovery_limit_is_explicit(self):
        for i in range(4):
            self.write(f"run{i}/job-result.json", {"exit_code": 0})
        with patch("agentharness.viewer.MAX_RUNS", 2):
            result = self.store.listing()
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["runs"]), 2)


@unittest.skipUnless(os.name == "posix", "secure POSIX reader")
class ViewerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "run/evidence"
        path.mkdir(parents=True)
        (path / "result.json").write_text(json.dumps({"status": "verified", "changed_files": ["<img src=x onerror=alert(1)>.py"], "plan": {"goal": "<script>alert(1)</script>"}}))
        (path / "messages.json").write_text("DO_NOT_SERVE_TRANSCRIPTS")
        self.store = RunStore(self.temp.name)
        self.server = ViewerServer(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        value = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return value

    def test_loopback_assets_api_and_security_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Run history", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        listing = json.loads(self.request("/api/runs")[2])
        detail = json.loads(self.request("/api/runs/" + listing["runs"][0]["id"])[2])
        self.assertEqual(detail["status"], "verified")
        self.assertNotIn("DO_NOT_SERVE_TRANSCRIPTS", json.dumps(detail))
        js = self.request("/app.js")[2].decode()
        self.assertNotIn(".innerHTML", js)
        self.assertIn("textContent", js)
        self.assertEqual(self.request("/style.css")[0], 200)
        self.assertEqual(self.request("/", "HEAD")[2], b"")

    def test_no_filesystem_routes_queries_or_mutations(self):
        for path in ("/etc/passwd", "/api/runs/../../etc/passwd", "/api/runs/%2e%2e", "/api/runs?path=/etc/passwd", "/messages.json", "/api/runs/" + "0" * 24):
            self.assertIn(self.request(path)[0], (400, 404))
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request("/api/runs", method)[0], 405)

    def test_host_origin_and_cross_site_guard(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "localhost:" + str(self.server.server_port)}):
            self.assertEqual(self.request("/api/runs", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/runs", headers={"Origin": f"http://127.0.0.1:{self.server.server_port}"})[0], 200)

    def test_invalid_root_and_port_fail_cleanly(self):
        with self.assertRaises(SystemExit) as cm:
            main(["--runs", self.temp.name + "/missing"])
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(SystemExit):
            main(["--runs", self.temp.name, "--port", "-1"])


if __name__ == "__main__":
    unittest.main()
