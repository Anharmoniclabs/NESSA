"""Cloud fallback: switching, cooldowns, fatal errors and token loading (no network)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentharness import cloud
from agentharness.llm import ContextOverflow, ModelError, Reply


class Fake:
    def __init__(self, model, *outcomes):
        self.model, self.base_url, self.outcomes, self.calls = model, "http://x", list(outcomes), 0

    def chat(self, messages, tools=None, tool_names=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return Reply(outcome, [], False)


class Fallback(unittest.TestCase):
    def test_uses_first_available_and_reports_switch(self):
        switches = []
        a, b = Fake("cloud", ModelError("HTTP 503: busy")), Fake("local", "hi")
        client = cloud.FallbackClient([a, b], on_switch=lambda *s: switches.append(s[:2]))
        self.assertEqual(client.chat([]).content, "hi")
        self.assertEqual(client.model, "local")
        self.assertEqual(switches, [("cloud", "local")])

    def test_transient_failure_cools_down_then_retries(self):
        now = [0.0]
        a, b = Fake("cloud", ModelError("HTTP 503"), "back"), Fake("local", "hi")
        client = cloud.FallbackClient([a, b], clock=lambda: now[0])
        client.chat([])
        client.chat([])
        self.assertEqual(a.calls, 1)  # skipped during cooldown
        now[0] = cloud.COOLDOWN + 1
        self.assertEqual(client.chat([]).content, "back")

    def test_exhausted_credits_skip_for_the_session(self):
        now = [0.0]
        a = Fake("cloud", ModelError("HTTP 402: You have depleted your monthly included credits"))
        client = cloud.FallbackClient([a, Fake("local", "hi")], clock=lambda: now[0])
        client.chat([])
        now[0] = 10 ** 9
        client.chat([])
        self.assertEqual(a.calls, 1)

    def test_context_overflow_is_not_hidden_by_switching(self):
        client = cloud.FallbackClient([Fake("cloud", ContextOverflow("context length")), Fake("local", "hi")])
        with self.assertRaises(ContextOverflow):
            client.chat([])

    def test_all_unavailable_is_an_error(self):
        client = cloud.FallbackClient([Fake("a", ModelError("x")), Fake("b", ModelError("y"))])
        with self.assertRaisesRegex(ModelError, "All models unavailable"):
            client.chat([])

    def test_on_delta_reaches_every_client(self):
        a, b = Fake("a", "x"), Fake("b", "y")
        client = cloud.FallbackClient([a, b])
        client.on_delta = print
        self.assertIs(a.on_delta, print)
        self.assertIs(b.on_delta, print)


class Token(unittest.TestCase):
    def test_file_token_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HF_TOKEN": ""}):
            good, bad = Path(tmp, "HF"), Path(tmp, "bad")
            good.write_text("hf_abc\n")
            bad.write_text("nope")
            self.assertEqual(cloud.load_token(good), "hf_abc")
            with self.assertRaises(ValueError):
                cloud.load_token(bad)
            with self.assertRaises(ValueError):
                cloud.load_token(Path(tmp, "missing"))

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_env"}):
            self.assertEqual(cloud.load_token("/nonexistent"), "hf_env")

    def test_cloud_clients_are_remote_and_fail_fast(self):
        [client] = cloud.cloud_clients("hf_x", ("org/m",))
        self.assertEqual((client.base_url, client.retries), (cloud.HF_ROUTER, 1))


if __name__ == "__main__":
    unittest.main()
