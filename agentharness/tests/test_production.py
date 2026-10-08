import json
from pathlib import Path
import sys
import tempfile
import unittest
from agentharness.production import Production
from agentharness.workspace import ToolError


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.p=Production(self.root)

    def task(self,name='render',dependencies=None,checks=True,code=None,outputs=None,assets=None):
        self.p.update('task',dict(id=name,argv=[sys.executable,'-c',code or "open('frame.txt','w').write('frame')"],
            dependencies=dependencies or [],assets=assets or [],outputs=outputs or ['frame.txt'],
            checks=[[sys.executable,'-c',"assert open('frame.txt').read()=='frame'"]] if checks else [],timeout=10))

    def test_fresh_outputs_and_explicit_checks(self):
        self.task();row=self.p.run('render')
        self.assertEqual(row['status'],'verified')
        self.assertEqual(self.p.status()['tasks'][0]['status'],'verified')
        self.task('noop',code='pass')
        self.assertEqual(self.p.run('noop')['status'],'failed')
        self.assertEqual(self.p.status()['tasks'][1]['last_run'],'failed')

    def test_missing_artifacts_and_no_checks_are_not_verified(self):
        self.task(code='pass');self.assertEqual(self.p.run('render')['status'],'failed')
        self.task(checks=False);self.assertEqual(self.p.run('render')['status'],'completed')

    def test_dependency_and_asset_invalidation(self):
        source=self.root/'source.txt';source.write_text('v1')
        self.p.update('asset',dict(id='hero',path='source.txt'))
        self.task('a',assets=['hero']);self.task('b',dependencies=['a'],code="open('b.txt','w').write('b')",outputs=['b.txt'])
        with self.assertRaises(ToolError):self.p.run('b')
        self.p.run('a');self.p.run('b')
        source.write_text('v2')
        self.assertEqual(self.p.status()['tasks'][0]['status'],'blocked')
        self.p.update('asset',dict(id='hero',path='source.txt'))
        state=self.p.status()
        self.assertEqual(state['assets']['hero']['version'],2)
        versions=self.p.read()['assets']['hero']
        self.assertEqual(self.p.local(versions[0]['snapshot']).read_text(),'v1')
        self.assertEqual(self.p.local(versions[1]['snapshot']).read_text(),'v2')
        self.assertEqual([t['status'] for t in state['tasks']],['ready','blocked'])

    def test_cycle_and_outside_paths_rejected_atomically(self):
        self.task('a');self.task('b',dependencies=['a'])
        with self.assertRaises(ToolError):self.task('a',dependencies=['b'])
        self.assertEqual(self.p.read()['tasks']['a']['dependencies'],[])
        with self.assertRaises(ToolError):self.task('escape',outputs=['../outside'])
        self.assertNotIn('escape',self.p.read()['tasks'])

    def test_comparison_records_real_repeats_never_promotes(self):
        self.task('a');self.task('b')
        for _ in range(3):
            self.assertEqual(self.p.run('a')['status'],'verified')
            self.assertEqual(self.p.run('b')['status'],'verified')
        trial=self.p.compare('a','b')
        self.assertEqual(trial['samples'],[3,3])
        self.assertEqual(trial['status'],'timing_evidence')
        self.assertFalse(trial['promoted'])
        self.assertIn('unassessed',trial['quality'])
        self.assertEqual(len(trial['run_ids'][0]),3)

    def test_running_record_prevents_silent_replay(self):
        self.task()
        with self.p.edit() as state:
            state['runs'].append(dict(id='interrupted',task='render',status='running'))
        with self.assertRaises(ToolError):self.p.run('render')

    def test_review_is_artifact_bound_agent_note(self):
        (self.root/'frame.txt').write_text('reviewed')
        self.p.update('review',dict(path='frame.txt',verdict='accepted',notes='User asked for this version'))
        row=self.p.read()['reviews'][0]
        self.assertEqual(row['source'],'agent_recorded_note')
        self.assertEqual(len(row['sha256']),64)
        self.assertEqual(self.p.read()['runs'],[])

    def test_changed_dependency_output_invalidates_downstream(self):
        self.task('a',checks=False)
        self.task('b',dependencies=['a'],code="open('b.txt','w').write('b')",outputs=['b.txt'],checks=False)
        self.p.run('a');self.p.run('b')
        (self.root/'frame.txt').write_text('externally changed')
        self.assertEqual([t['status'] for t in self.p.status()['tasks']],['ready','blocked'])

    def test_failed_trials_are_not_silently_discarded(self):
        self.task('a');self.task('b')
        self.p.run('a');self.p.run('b')
        with self.p.edit() as state:
            row=dict(state['runs'][0]);row['status']='failed';state['runs'].append(row)
        with self.assertRaises(ToolError):self.p.compare('a','b')

    def test_check_failure_cannot_verify(self):
        self.task(code="open('frame.txt','w').write('wrong')")
        self.assertEqual(self.p.run('render')['status'],'failed')

    def test_interrupt_persists_unknown_then_requires_explicit_recovery(self):
        from unittest.mock import patch
        self.task()
        with patch('agentharness.production.run_command',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.p.run('render')
        row=self.p.read()['runs'][-1]
        self.assertEqual(row['status'],'outcome_unknown')
        with self.assertRaises(ToolError):self.p.run('render')
        self.p.recover(row['id'],'Test process stopped; no output created; safe to retry.')
        self.assertEqual(self.p.run('render')['status'],'verified')
        self.assertEqual(self.p.read()['runs'][0]['status'],'outcome_unknown')

    def test_interrupt_kills_command_process_group(self):
        from unittest.mock import patch,MagicMock
        from agentharness.checks import run_command
        proc=MagicMock();proc.pid=999999;proc.wait.side_effect=[KeyboardInterrupt,0]
        with patch('agentharness.checks.subprocess.Popen',return_value=proc), \
             patch('agentharness.checks.os.killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):run_command('unused',self.root)
        kill.assert_called_once()
