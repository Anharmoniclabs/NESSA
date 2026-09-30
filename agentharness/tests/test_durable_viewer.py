"""Composition checks: real durable artifacts projected through the read-only viewer."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agentharness.durable import DurableRun, StateStore
from agentharness.three_stage import ThreeStageConfig
from agentharness.viewer import RunStore
from agentharness.tests.test_agent import BUGGY, ScriptedClient, make_project
from agentharness.tests.test_code_proposals import GOOD, PLAN


@unittest.skipUnless(os.name == 'posix', 'durable lock and safe viewer require POSIX')
class DurableViewerCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = make_project(self.root)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.work = self.runs / 'repair'
        self.config = ThreeStageConfig(solve_mode='code-only', require_approval=False,
            allow_shell=False, allow_extract=False, max_steps=3, max_proposals=3, time_budget=120)
        self.viewer = RunStore(self.runs)
        self.addCleanup(self.viewer.close)

    def client(self, steps):
        client = ScriptedClient(steps)
        client.base_url = 'http://127.0.0.1:11434/v1'
        client.max_tokens, client.timeout, client.temperature = 768, 2, 0.0
        client.retries, client.api_key = 1, 'local'
        return client

    def snapshot(self):
        return {str(p.relative_to(self.work)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in self.work.rglob('*') if p.is_file()}

    def view(self):
        before = self.snapshot()
        self.viewer.updated = 0
        listing = self.viewer.listing()
        self.assertEqual(len(listing['runs']), 1)
        detail = self.viewer.detail(listing['runs'][0]['id'])
        self.assertEqual(before, self.snapshot(), 'Viewing may not mutate durable state/evidence')
        return detail

    def test_approved_pause_then_resume_exports_are_visible_without_mutation(self):
        paused = DurableRun.start(self.project, self.work, 'Fix addition',
            client=self.client([[('propose_plan', PLAN)]]), config=self.config, pause_after_approval=True)
        self.assertEqual(paused.status, 'paused')
        initial = self.view()
        self.assertEqual(initial['status'], 'in_progress_or_interrupted')
        self.assertTrue(initial['approved'])
        self.assertEqual(initial['plan']['files'], ['calc.py'])
        self.assertIsNone(initial['independent'])
        result = DurableRun.resume(self.work, client=self.client([GOOD, 'Controller facts recorded.']), steps=1)
        self.assertEqual(result.status, 'verified')
        final = self.view()
        self.assertEqual(final['status'], 'verified')
        self.assertEqual(final['status_source'], 'result.json')
        self.assertEqual(final['changed_files'], ['calc.py'])
        self.assertEqual(final['checks']['final']['tests']['status'], 'passed')
        self.assertIn('+    return a + b', final['patch'])
        self.assertFalse(final['advisory']['authoritative'])
        self.assertIsNone(final['independent'])
        self.assertEqual((self.project / 'calc.py').read_text(), BUGGY)
        before = self.snapshot()
        client = self.client(['must not run'])
        self.assertEqual(DurableRun.resume(self.work, client=client, steps=1).status, 'verified')
        self.assertEqual(client.seen, [])
        self.assertEqual(before, self.snapshot())

    def test_unexported_terminal_state_is_not_inferred_or_rewritten_by_viewer(self):
        def interrupt(name, state):
            if name == 'terminal':
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            DurableRun.start(self.project, self.work, 'Fix addition',
                client=self.client([[('propose_plan', PLAN)], GOOD, 'done']),
                config=self.config, boundary=interrupt)
        self.assertEqual(StateStore(self.work).load()['next_step'], 'terminal')
        self.assertFalse((self.work / 'evidence/result.json').exists())
        detail = self.view()
        self.assertEqual(detail['status'], 'in_progress_or_interrupted')
        self.assertIn('checkpoint, not final', detail['patch_source'])
        before = self.snapshot()
        client = self.client(['must not run'])
        self.assertEqual(DurableRun.resume(self.work, client=client, steps=1).status, 'verified')
        self.assertEqual(client.seen, [])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.view()['status'], 'in_progress_or_interrupted')
