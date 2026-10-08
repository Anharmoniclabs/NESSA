"""Guard the pilot's scoring against fallback and model output path escapes."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.cloud_harness import TASKS, check, run_attempt, write_files
from agentharness.llm import ModelError, Reply


class PilotTests(unittest.TestCase):
    def test_fixtures_fail_before_fix_and_pass_reference_solution(self):
        with tempfile.TemporaryDirectory() as tmp:
            for task in TASKS:
                for variant, expected in (('files',False),('gold',True)):
                    root=Path(tmp)/task['id']/variant
                    write_files(root,task[variant])
                    with self.subTest(task=task['id'],variant=variant):
                        self.assertEqual(check(root,task['public']+task['hidden'])['passed'],expected)

    def attempt(self, fallback=False, escape=False):
        task=TASKS[0]
        class Fake:
            def __init__(self, base_url, model, **kwargs):
                self.base_url,self.model,self.calls=base_url,model,[]
            def chat(self, *args):
                success=not (fallback and self.base_url.startswith('https'))
                self.calls.append(dict(success=success))
                if not success:
                    raise ModelError('HTTP 429')
                files={'../escaped.py':'bad'} if escape else task['gold']
                return Reply(json.dumps(files),[],False)
        with tempfile.TemporaryDirectory() as tmp, patch('benchmarks.cloud_harness.MeteredClient',Fake), contextlib.redirect_stdout(io.StringIO()):
            result=run_attempt(task,'org/model','raw',Path(tmp)/'attempt','test-token')
            self.assertFalse((Path(tmp)/'attempt/work/escaped.py').exists())
            return result

    def test_cloud_solution_counts_only_when_independent_grader_passes(self):
        result=self.attempt()
        self.assertTrue(result['cloud_pass'])
        self.assertTrue(result['source_unchanged'])

    def test_correct_local_fallback_is_not_a_cloud_success(self):
        result=self.attempt(fallback=True)
        self.assertTrue(result['hidden']['passed'])
        self.assertTrue(result['fallback_used'])
        self.assertFalse(result['cloud_pass'])

    def test_model_cannot_write_outside_task_file_allowlist(self):
        result=self.attempt(escape=True)
        self.assertEqual(result['status'],'error')
        self.assertFalse(result['cloud_pass'])


if __name__ == '__main__':
    unittest.main()
