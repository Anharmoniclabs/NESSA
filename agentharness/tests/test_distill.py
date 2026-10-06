"""Distillation capture: only cloud replies, outcome filtering, scrubbing, export format."""
import json
import tempfile
import unittest
from pathlib import Path

from agentharness import cloud, distill
from agentharness.llm import ModelError, Reply


class Teacher:
    base_url = cloud.HF_ROUTER

    def __init__(self, model="org/teacher", fail=False):
        self.model, self.fail = model, fail

    def chat(self, messages, tools=None, tool_names=None):
        if self.fail:
            raise ModelError("HTTP 503")
        raw = [{"id": "c0", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]
        return Reply("", [], True, raw)


class Student(Teacher):
    base_url = "http://127.0.0.1:11435/v1"

    def chat(self, messages, tools=None, tool_names=None):
        return Reply("local answer", [], False)


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rec = distill.Recorder(self.tmp)

    def test_records_cloud_replies_with_context_and_outcome(self):
        client = cloud.FallbackClient([Teacher(), Student()], recorder=self.rec)
        self.rec.begin("run-1")
        client.chat([{"role": "user", "content": "fix it, token=hf_" + "x" * 30}], [{"type": "function"}])
        self.rec.outcome("verified")
        out = self.tmp / "data.jsonl"
        self.assertEqual(distill.export(out, self.tmp), 1)
        row = json.loads(out.read_text())
        self.assertEqual(row["messages"][-1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertNotIn("hf_xxxx", json.dumps(row))
        self.assertEqual(row["tools"], [{"type": "function"}])

    def test_local_replies_are_never_recorded(self):
        client = cloud.FallbackClient([Teacher(fail=True), Student()], recorder=self.rec)
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(distill.stats(self.tmp)["turns"], 0)

    def test_failed_and_unfinished_runs_are_not_exported(self):
        client = cloud.FallbackClient([Teacher()], recorder=self.rec)
        for run, status in (("bad", "failed_checks"), ("open", None)):
            self.rec.begin(run)
            client.chat([{"role": "user", "content": run}])
            if status:
                self.rec.outcome(status)
        self.assertEqual(distill.export(self.tmp / "o.jsonl", self.tmp), 0)
        self.assertEqual(distill.export(self.tmp / "o.jsonl", self.tmp, include_unfinished=True), 1)

    def test_duplicates_and_long_history(self):
        client = cloud.FallbackClient([Teacher()], recorder=self.rec)
        self.rec.begin("r")
        history = [{"role": "system", "content": "s"}] + [{"role": "user", "content": "x" * 5000}] * 10
        client.chat(history)
        client.chat(history)
        self.rec.outcome("completed")
        out = self.tmp / "o.jsonl"
        self.assertEqual(distill.export(out, self.tmp, max_chars=12000), 1)
        row = json.loads(out.read_text())
        self.assertEqual(row["messages"][0]["role"], "system")
        self.assertLess(len(json.dumps(row)), 14000)

    def test_scrub_private_key_and_password(self):
        text = "password = hunter2secret\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
        clean = distill.scrub(text)
        self.assertNotIn("hunter2secret", clean)
        self.assertNotIn("abc", clean)


if __name__ == "__main__":
    unittest.main()
