"""Details-window panels are plain functions over the activity feed."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentharness import panels


def ev(event, /, **data):
    return dict(event=event, time=1_700_000_000, data=data)


class Panels(unittest.TestCase):
    def test_timeline_shows_meaningful_steps_and_hides_internal_chatter(self):
        feed = [ev('model_started', model='m'), ev('prompt_budget', bytes=5, tools=3, model='m'),
                ev('plan', plan={'goal': 'fix add'}), ev('approval', approved=True),
                ev('tool', name='read_file', args={'path': 'calc.py'}), ev('check', name='tests', status='failed',
                                                                            phase='verify'),
                ev('end', status='verified')]
        out = panels.timeline(feed)
        for fragment in ('plan proposed: fix add', 'approval granted', 'tool read_file', 'check tests failed (verify)',
                         'run ended: verified'):
            self.assertIn(fragment, out)
        self.assertNotIn('prompt_budget', out)
        self.assertEqual(panels.timeline([]), 'No steps yet.')

    def test_checks_use_latest_status_per_check_and_flag_integrity_events(self):
        feed = [ev('check', name='tests', status='failed', phase='verify', exit_code=1),
                ev('check', name='tests', status='passed', phase='verify', exit_code=0),
                ev('check', name='syntax', status='passed', phase='continuous'),
                ev('verification_receipt', workspace_sha='a' * 64, checks_sha='b' * 64, statuses={'tests': 'passed'}),
                ev('repeated_failure', failing=['tests'])]
        out = panels.checks(feed, {'status': 'verified'})
        self.assertIn('✓ tests · verify · passed', out)
        self.assertNotIn('failed', out.split('INTEGRITY')[0])
        self.assertIn('workspace ' + 'a' * 16, out)
        self.assertIn('repeated failure', out)
        self.assertIn('verified', out)
        self.assertEqual(panels.checks([]), 'No checks have run yet.')

    def test_failing_check_is_marked(self):
        out = panels.checks([ev('check', name='tests', status='timeout', phase='verify')])
        self.assertIn('✗ tests · verify · timeout', out)

    def test_browser_captures_are_read_from_the_evidence_directory(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / 'browser').mkdir()
        (tmp / 'browser' / 'before.json').write_text(json.dumps(
            dict(label='before', url='http://localhost:3000/', console=['error: x'], failed_requests=[])))
        (tmp / 'browser' / 'broken.json').write_text('{nope')
        out = panels.checks([], None, str(tmp))
        self.assertIn('before · http://localhost:3000/ · 1 console, 0 failed requests', out)
        self.assertNotIn('broken', out)

    def test_context_reviewer_and_processes(self):
        feed = [ev('prompt_budget', bytes=12345, tools=9, model='lfm'), ev('context_compacted'),
                ev('review', phase='final', edit=2, status='ok', notes='Looks fine.'),
                ev('skill', name='observe-local-app'),
                ev('tool', name='dev_start', args={'name': 'web'}, output='web: started pid 42')]
        context = panels.context(feed)
        self.assertIn('12,345 bytes', context)
        self.assertIn('Compactions seen: 1', context)
        self.assertIn('not tokens', context)
        self.assertIn('Looks fine.', panels.reviewer(feed))
        self.assertIn('cannot verify', panels.reviewer(feed))
        procs = panels.processes(feed)
        self.assertIn('dev_start', procs)
        self.assertIn('skill observe-local-app', procs)
        self.assertIn('No reviewer notes', panels.reviewer([]))
        self.assertIn('No model request', panels.context([]))


if __name__ == '__main__':
    unittest.main()
