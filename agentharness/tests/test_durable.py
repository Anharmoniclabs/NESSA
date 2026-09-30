"""Durable boundaries using scripted clients and local fake HTTP; no inference."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from agentharness.durable import DurableRun, ResumeError, StateStore
from agentharness.three_stage import ThreeStageConfig
from agentharness.workspace import sha
from agentharness.tests.test_agent import BUGGY, ScriptedClient, make_project
from agentharness.tests.test_code_proposals import GOOD, PLAN


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = make_project(self.tmp)
        self.work = self.tmp / 'work'
        self.cfg = ThreeStageConfig(solve_mode='code-only', require_approval=False,
                                   allow_shell=False, allow_extract=False,
                                   max_steps=3, max_proposals=3, time_budget=120)

    def client(self, steps):
        client = ScriptedClient(steps)
        client.base_url = 'http://127.0.0.1:11434/v1'
        client.max_tokens = 768
        client.timeout = 2
        client.temperature = 0.0
        client.retries = 1
        client.api_key = 'local'
        return client

    def start(self, steps=None, **kwargs):
        return DurableRun.start(self.project, self.work, 'Fix addition',
            client=self.client(steps or [[('propose_plan', PLAN)], GOOD, 'done']),
            config=self.cfg, **kwargs)

    def resume(self, steps=None, **kwargs):
        return DurableRun.resume(self.work, client=self.client(steps or [GOOD, 'done']),
                                 steps=kwargs.pop('steps_limit', 1), **kwargs)

    def state(self):
        return StateStore(self.work).load()

    def interrupt(self, target):
        def boundary(name, state):
            if name == target:
                raise KeyboardInterrupt()
        return boundary

    def test_approved_pause_resume_and_terminal_exact_noop(self):
        result = self.start(pause_after_approval=True)
        self.assertEqual(result.status, 'paused')
        state = self.state()
        self.assertTrue(state['approval']['approved'])
        self.assertEqual(state['approval']['plan'], PLAN)
        self.assertEqual(state['next_step'], 'model')
        self.assertEqual(state['config']['allow_shell'], False)
        self.assertIn('source', state['hashes'])
        self.assertIn('checks', state['runtime'])
        result = self.resume()
        self.assertEqual(result.status, 'verified', result.summary)
        self.assertEqual(result.steps, 1)
        self.assertEqual((self.project / 'calc.py').read_text(), BUGGY)
        before = {str(p): p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
        client = self.client(['must never run'])
        again = DurableRun.resume(self.work, client=client, steps=1)
        self.assertEqual(again.to_dict(), result.to_dict())
        self.assertEqual(client.seen, [])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.work.rglob('*') if p.is_file()})

    def test_rejection_and_cancellation_never_resume(self):
        self.cfg.require_approval = True
        result = self.start(approver=lambda plan: (False, ''))
        self.assertEqual(result.status, 'rejected')
        client = self.client(['must never run'])
        self.assertEqual(DurableRun.resume(self.work, client=client, steps=1).status, 'rejected')
        self.assertFalse(client.seen)
        other = self.tmp / 'other'
        self.work = other
        self.cfg.require_approval = False
        self.start(pause_after_approval=True)
        DurableRun.cancel(self.work)
        before = StateStore(self.work).load()
        with self.assertRaisesRegex(ResumeError, 'cancelled'):
            self.resume()
        self.assertEqual(before, StateStore(self.work).load())

    def test_noop_source_proposals_cannot_report_success_or_reset_budget(self):
        self.start(pause_after_approval=True)
        for i in range(3):
            result = self.resume([BUGGY, 'done'])
            self.assertEqual(result.steps, i + 1)
        self.assertEqual(result.status, 'no_change')
        self.assertEqual(result.patch, '')
        self.assertEqual(self.state()['journal'], [])
        self.assertEqual(self.resume().status, 'no_change')

    def test_exact_edit_reconciliation_before_and_after_atomic_replace(self):
        for boundary in ('edit_pending', 'edit_replaced', 'edit_committed', 'checks_pending', 'checks_recorded'):
            with self.subTest(boundary=boundary):
                self.work = self.tmp / boundary
                with self.assertRaises(KeyboardInterrupt):
                    self.start(boundary=self.interrupt(boundary))
                result = self.resume(['done'])
                self.assertEqual(result.status, 'verified', result.summary)
                self.assertEqual(result.steps, 1)
                self.assertEqual(len(self.state()['journal']), 1)
                self.assertEqual(self.state()['proposals'], 1)
                self.assertEqual((self.work / 'repo/calc.py').read_text(), GOOD)
                self.assertTrue((self.work / 'evidence/checkpoints/edit-0001.diff').exists())

    def test_lost_model_response_is_discarded_and_turn_is_charged(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(boundary=self.interrupt('model_pending'))
        self.assertEqual(self.state()['steps'], 1)
        result = self.resume()
        self.assertEqual(result.status, 'verified')
        self.assertEqual(result.steps, 2)
        self.assertTrue(any(r['outcome'] == 'discarded_unknown_response' for r in self.state()['receipts']))

    def test_durable_reply_is_validated_without_second_model_call(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(boundary=self.interrupt('model_received'))
        client = self.client(['done'])
        result = DurableRun.resume(self.work, client=client, steps=1)
        self.assertEqual(result.status, 'verified')
        self.assertEqual(len(client.seen), 1)  # respond only

    def test_source_baseline_workspace_and_mode_drift_fail_closed(self):
        for target in ('source', 'baseline', 'repo', 'mode'):
            with self.subTest(target=target):
                self.work = self.tmp / target
                self.start(pause_after_approval=True)
                path = (self.project if target == 'source' else self.work / ('repo' if target == 'mode' else target)) / 'calc.py'
                old = path.read_bytes()
                mode = path.stat().st_mode
                if target == 'mode':
                    path.chmod(0o700)
                else:
                    path.write_text('def add(a,b): return 99\n')
                client = self.client([GOOD])
                with self.assertRaises(ResumeError):
                    DurableRun.resume(self.work, client=client, steps=1)
                self.assertFalse(client.seen)
                path.write_bytes(old)
                path.chmod(mode)

    def test_unknown_edit_postimage_refused_without_mutating_evidence(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(boundary=self.interrupt('edit_pending'))
        (self.work / 'repo/calc.py').write_text('def add(a,b): return 999\n')
        before = self.state()
        with self.assertRaises(ResumeError):
            self.resume()
        self.assertEqual(self.state(), before)

    def test_runtime_change_and_missing_config_refused(self):
        self.start(pause_after_approval=True)
        client = self.client([GOOD])
        client.max_tokens += 1
        with self.assertRaises(ResumeError):
            DurableRun.resume(self.work, client=client, steps=1)
        state = self.state()
        del state['config']['allow_shell']
        StateStore(self.work).save(state)
        with self.assertRaises(ResumeError):
            self.resume()

    def test_corrupt_pointer_generation_and_unsupported_schema_refused(self):
        self.start(pause_after_approval=True)
        store = StateStore(self.work)
        pointer = store.pointer.read_bytes()
        store.pointer.write_text('{}')
        with self.assertRaises(ResumeError):
            store.load()
        store.pointer.write_bytes(pointer)
        current = json.loads(pointer)
        generation = store.directory / current['file']
        generation.write_bytes(generation.read_bytes() + b' ')
        with self.assertRaises(ResumeError):
            self.resume()

    def test_orphan_generation_not_promoted_and_event_log_is_not_truncated(self):
        self.start(pause_after_approval=True)
        store = StateStore(self.work)
        state = store.load()
        (store.directory / 'state-99999999.json').write_text('{}')
        self.assertEqual(store.load(), state)
        events = (self.work / 'evidence/events.jsonl').read_text()
        self.resume()
        self.assertTrue((self.work / 'evidence/events.jsonl').read_text().startswith(events))

    def test_only_explicit_bounded_resumption_and_supported_scope(self):
        self.start(pause_after_approval=True)
        for value in (0, -1, True, 10001):
            with self.assertRaises((ResumeError, ValueError)):
                DurableRun.resume(self.work, client=self.client([GOOD]), steps=value)
        self.work = self.tmp / 'unsafe'
        self.cfg.allow_shell = True
        with self.assertRaises((ResumeError, ValueError)):
            self.start()
        self.assertFalse(self.work.exists())

    def test_lock_blocks_concurrent_resumer(self):
        self.start(pause_after_approval=True)
        with StateStore(self.work).locked():
            with self.assertRaisesRegex(ResumeError, 'active'):
                self.resume()

    def test_interrupted_unapproved_intake_never_resumes(self):
        def interrupted(plan):
            raise KeyboardInterrupt()
        self.cfg.require_approval = True
        with self.assertRaises(KeyboardInterrupt):
            self.start(approver=interrupted)
        with self.assertRaisesRegex(ResumeError, 'approval'):
            self.resume()


    def test_competing_start_cannot_reinitialize_an_existing_run(self):
        original = StateStore.locked
        entered = False
        nested = {}

        @contextmanager
        def interleave(store):
            nonlocal entered
            if not entered:
                entered = True
                nested['result'] = self.start()
                nested['pointer'] = StateStore(self.work).pointer.read_bytes()
            with original(store):
                yield

        with patch.object(StateStore, 'locked', interleave):
            with self.assertRaisesRegex(ResumeError, 'initialized'):
                self.start(pause_after_approval=True)
        self.assertEqual(nested['result'].status, 'verified')
        self.assertEqual((self.work / 'repo/calc.py').read_text(), GOOD)
        self.assertEqual(nested['pointer'], StateStore(self.work).pointer.read_bytes())

    def test_unknown_operation_reservation_can_exhaust_time_without_new_model(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(boundary=self.interrupt('model_pending'))
        state = self.state()
        state['active_seconds'] = 119
        StateStore(self.work).save(state)
        client = self.client(['must not run'])
        result = DurableRun.resume(self.work, client=client, steps=1)
        self.assertEqual(result.status, 'budget_exhausted')
        self.assertEqual(result.steps, 1)
        self.assertFalse(client.seen)
        self.assertEqual(result.patch, '')

    def test_interrupted_advisory_response_is_not_reissued(self):
        def interrupt(messages):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.start([[('propose_plan', PLAN)], GOOD, interrupt])
        client = self.client(['must not run'])
        result = DurableRun.resume(self.work, client=client, steps=1)
        self.assertEqual(result.status, 'verified')
        self.assertEqual(self.state()['response_stage']['status'], 'interrupted')
        self.assertFalse(client.seen)

    def test_credentials_and_arbitrary_check_recipes_are_not_restorable(self):
        client = self.client([GOOD])
        client.api_key = 'do-not-persist-this'
        with self.assertRaises(ResumeError):
            DurableRun.start(self.project, self.work, 'Fix addition', client=client, config=self.cfg)
        self.assertFalse(self.work.exists())
        self.start(pause_after_approval=True)
        state = self.state()
        state['runtime']['checks']['tests'] = ['/bin/sh', '-c', 'echo should-never-run']
        StateStore(self.work).save(state)
        with self.assertRaises((ResumeError, ValueError)):
            self.resume()

    def test_truncated_and_interface_breaking_saved_replies_are_rejected(self):
        for source in ('def add(a): return a', '{"name":"run_command"}', 'def add(:'):
            with self.subTest(source=source):
                self.work = self.tmp / str(len(list(self.tmp.iterdir())))
                self.start(pause_after_approval=True)
                result = self.resume([source])
                self.assertEqual(result.status, 'paused')
                self.assertEqual(result.patch, '')
                self.assertEqual(self.state()['receipts'][-1]['outcome'], 'proposal_rejected')


    def test_pending_source_and_journal_contracts_cannot_be_bypassed(self):
        for damage in ('oversized', 'journal_path', 'journal_before', 'journal_after'):
            with self.subTest(damage=damage):
                self.work = self.tmp / damage
                with self.assertRaises(KeyboardInterrupt):
                    self.start(boundary=self.interrupt('edit_pending'))
                state = self.state()
                pending = state['pending']
                if damage == 'oversized':
                    pending['source'] = GOOD + '#' + 'x' * 12500 + '\n'
                    pending['journal_entry']['after'] = sha(pending['source'])
                else:
                    pending['journal_entry'][damage.removeprefix('journal_')] = 'wrong'
                StateStore(self.work).save(state)
                with self.assertRaises(ResumeError):
                    self.resume()
                self.assertEqual((self.work / 'repo/calc.py').read_text(), BUGGY)

    def test_empty_success_and_all_skipped_tests_cannot_verify(self):
        result = self.start([[('propose_plan', PLAN)], GOOD + '\nimport os\nos._exit(0)\n', 'done'])
        self.assertNotEqual(result.status, 'verified')
        self.assertIn('no_tests', result.checks['final']['tests'])
        self.work = self.tmp / 'skipped'
        tests = self.project / 'tests/test_calc.py'
        tests.write_text(tests.read_text().replace('    def test_add', "    @unittest.skip('fixture')\n    def test_add"))
        result = self.start()
        self.assertNotEqual(result.status, 'verified')
        self.assertIn('no_tests', result.checks['final']['tests'])

    def test_late_intake_response_terminates_without_approval_or_edit(self):
        self.cfg.time_budget = 0.1
        def late(messages):
            time.sleep(0.15)
            return [('propose_plan', PLAN)]
        result = self.start([late])
        self.assertEqual(result.status, 'budget_exhausted')
        self.assertFalse(self.state()['approval']['approved'])
        self.assertEqual(result.patch, '')
        self.assertEqual(self.resume().status, 'budget_exhausted')


    def test_saved_response_status_cannot_replace_controller_verification(self):
        for damage in ('respond', 'checks_recorded', 'missing_check', 'counts_type', 'failed_count', 'exit_code', 'command'):
            with self.subTest(damage=damage):
                self.work = self.tmp / damage
                boundary = 'edit_committed' if damage in ('respond', 'checks_recorded') else 'checks_recorded'
                with self.assertRaises(KeyboardInterrupt):
                    self.start(boundary=self.interrupt(boundary))
                state = self.state()
                if damage in ('respond', 'checks_recorded'):
                    state['next_step'] = damage
                    state['final_status'] = 'verified'
                elif damage == 'missing_check':
                    del state['final']['tests']
                elif damage == 'failed_count':
                    state['final']['tests']['counts']['failed'] = 1
                elif damage == 'exit_code':
                    state['final']['tests']['exit_code'] = 1
                elif damage == 'command':
                    state['final']['tests']['command'] = 'echo unregistered'
                else:
                    state['final']['tests']['counts'] = 'bad'
                StateStore(self.work).save(state)
                client = self.client(['must never run'])
                with self.assertRaises(ResumeError):
                    DurableRun.resume(self.work, client=client, steps=1)
                self.assertFalse(client.seen)

    def test_pending_edit_needs_charged_attempt_and_context_safe_size(self):
        for damage in ('attempt', 'context_bound'):
            with self.subTest(damage=damage):
                self.work = self.tmp / damage
                self.cfg.context_chars = 8000
                with self.assertRaises(KeyboardInterrupt):
                    self.start(boundary=self.interrupt('edit_pending'))
                state = self.state()
                if damage == 'attempt':
                    state['steps'] = 0
                else:
                    state['pending']['source'] = GOOD + '#' + 'x' * 9000 + '\n'
                    state['pending']['journal_entry']['after'] = sha(state['pending']['source'])
                StateStore(self.work).save(state)
                with self.assertRaises(ResumeError):
                    self.resume()
                self.assertEqual((self.work / 'repo/calc.py').read_text(), BUGGY)

    def test_terminal_counter_and_empty_check_argv_are_invalid(self):
        self.start()
        state = self.state()
        state['result']['steps'] += 1
        StateStore(self.work).save(state)
        with self.assertRaises(ResumeError):
            self.resume()
        self.work = self.tmp / 'argv'
        self.start(pause_after_approval=True)
        state = self.state()
        state['runtime']['checks']['tests'] = []
        StateStore(self.work).save(state)
        with self.assertRaises(ResumeError):
            self.resume()


    def test_concurrent_post_replace_change_is_not_adopted_as_authority(self):
        def changed_after_replace(name, state):
            if name == 'edit_replaced':
                (self.work / 'repo/calc.py').write_text('def add(a, b): return 999\n')
        with self.assertRaisesRegex(ResumeError, 'during replacement'):
            self.start(boundary=changed_after_replace)
        state = self.state()
        self.assertEqual(state['next_step'], 'edit_pending')
        self.assertEqual(state['proposals'], 0)
        self.assertEqual(state['journal'], [])
        with self.assertRaises(ResumeError):
            self.resume()


if __name__ == '__main__':
    unittest.main()
