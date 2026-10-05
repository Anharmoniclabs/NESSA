"""Verification receipts bind evidence to the workspace and checks that produced it."""
import json
import unittest
from pathlib import Path

from agentharness import receipt as receipts
from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.tests.test_agent import Base, ScriptedClient, UNITTEST


FIX = ('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'})
BREAK = ('replace_in_file', {'path': 'calc.py', 'old': 'a - b', 'new': 'a * b'})


class Receipts(Base):
    def events(self, result):
        return [json.loads(l) for l in (Path(result.evidence_dir) / 'events.jsonl').read_text().splitlines()]

    def test_verified_run_carries_receipt_matching_final_files(self):
        result = self.agent([[('read_file', {'path': 'calc.py'})], [FIX],
                             [('finish', {'summary': 'done'})]], plan_first=False).run('fix add')
        self.assertEqual(result.status, 'verified')
        self.assertEqual(result.receipt['statuses']['tests'], 'passed')
        self.assertEqual(result.receipt['workspace_sha'], receipts.workspace_fingerprint(self.ws))
        self.assertEqual(json.loads((Path(result.evidence_dir) / 'result.json').read_text())['receipt'],
                         result.receipt)

    def test_receipt_goes_stale_when_files_or_check_definitions_change(self):
        self.ws.read('calc.py'); self.ws.replace('calc.py', 'a - b', 'a + b')
        runner = CheckRunner({'syntax': syntax_check, 'tests': UNITTEST}, timeout=60)
        results = {n: runner.run(n, self.ws) for n in ('syntax', 'tests')}
        receipt = receipts.make_receipt(self.ws, runner, results)
        self.assertTrue(receipts.matches_current(receipt, self.ws, runner))
        runner.checks['tests'] = UNITTEST + ' -v'
        self.assertFalse(receipts.matches_current(receipt, self.ws, runner))
        runner.checks['tests'] = UNITTEST
        self.ws.replace('calc.py', 'a + b', 'a + b + 0')
        self.assertFalse(receipts.matches_current(receipt, self.ws, runner))

    def test_finish_rejected_when_workspace_moves_after_checks(self):
        self.ws.read('calc.py'); self.ws.replace('calc.py', 'a - b', 'a + b')
        moved = []

        def grader(ws, task, summary):  # runs after the checks, before the verdict
            if not moved:
                moved.append(1)
                ws.replace('calc.py', 'a + b', 'a + b + 0')
            return {'status': 'passed', 'evidence': 'ok'}

        checks = CheckRunner({'syntax': syntax_check, 'tests': UNITTEST}, timeout=60)
        agent = Agent(ScriptedClient([[('finish', {'summary': 'one'})], [('finish', {'summary': 'two'})]]),
                      self.ws, checks=checks, acceptance_grader=grader,
                      config=AgentConfig(require_approval=False, plan_first=False, baseline_checks=False))
        result = agent.run('fix add')
        names = [e['event'] for e in self.events(result)]
        self.assertIn('stale_receipt', names)
        self.assertEqual(result.status, 'verified')  # only after a second, current verification
        self.assertEqual(result.receipt['workspace_sha'], receipts.workspace_fingerprint(self.ws))

    def test_identical_failure_without_new_edit_stops_early(self):
        result = self.agent([[('read_file', {'path': 'calc.py'})], [BREAK],
                             [('finish', {'summary': 'a'})], [('finish', {'summary': 'b'})],
                             [('finish', {'summary': 'c'})]], plan_first=False, finish_retries=5).run('fix add')
        self.assertEqual(result.status, 'failed_checks')
        self.assertEqual([e['event'] for e in self.events(result)].count('repeated_failure'), 1)

    def test_a_changed_failure_is_new_evidence_and_still_retries(self):
        result = self.agent([[('read_file', {'path': 'calc.py'})], [BREAK],
                             [('finish', {'summary': 'a'})],
                             [('replace_in_file', {'path': 'calc.py', 'old': 'a * b', 'new': 'a + b'})],
                             [('finish', {'summary': 'b'})]], plan_first=False, finish_retries=2).run('fix add')
        self.assertEqual(result.status, 'verified')


class DevCleanup(Base):
    def test_stop_all_reports_failures_and_still_stops_the_rest(self):
        from unittest.mock import patch
        from agentharness.dev import DevProcessManager
        manager = DevProcessManager(self.ws, self.tmp / 'evidence')
        manager.processes = {'stuck': object(), 'fine': object()}
        stopped = []

        def stop(name):
            if name == 'stuck':
                raise PermissionError('not permitted')
            stopped.append(name)

        with patch.object(manager, 'stop', side_effect=stop):
            failures = manager.stop_all()
        self.assertEqual(stopped, ['fine'])
        self.assertEqual(list(failures), ['stuck'])
        self.assertIn('PermissionError', failures['stuck'])


class Report(Base):
    def test_report_shows_checks_and_receipt_and_escapes_event_text(self):
        from agentharness import report
        result = self.agent([[('read_file', {'path': 'calc.py'})], [FIX],
                             [('finish', {'summary': 'done'})]], plan_first=False).run('fix add')
        evidence = Path(result.evidence_dir)
        with (evidence / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'seq': 999, 't': 0, 'event': 'repeated_failure',
                                'failing': ['<script>alert(1)</script>']}) + '\n')
        page = report.render(evidence)
        self.assertIn(result.receipt['workspace_sha'][:16], page)
        self.assertIn('tests', page)
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_report_command_fails_cleanly_without_evidence(self):
        from agentharness.__main__ import main
        self.assertEqual(main(['report', str(self.tmp / 'nowhere')]), 1)


class CommandLine(unittest.TestCase):
    def test_every_subcommand_parses_with_its_required_arguments(self):
        from agentharness.__main__ import build_parser
        parser = build_parser()
        cases = {'run': ['run', 'proj', 'task'], 'chat': ['chat', 'proj'], 'resume': ['resume', 'work'],
                 'batch': ['batch', '--comp', 'c'], 'extract': ['extract', 'a.pdf'], 'skills': ['skills', 'proj'],
                 'config': ['config', 'proj'], 'lesson': ['lesson', 'list', 'proj'],
                 'report': ['report', 'ev'], 'doctor': ['doctor']}
        for name, argv in cases.items():
            self.assertEqual(parser.parse_args(argv).cmd, name)


class ToolContract(unittest.TestCase):
    KINDS = {'read', 'edit', 'check', 'dev', 'control', 'mcp'}

    def test_every_tool_has_schema_kind_and_unique_name(self):
        from agentharness.tools import build_tools
        tools = build_tools()
        for name, tool in tools.items():
            self.assertEqual(name, tool.name)
            self.assertTrue(tool.description.strip(), name)
            self.assertIn(tool.kind, self.KINDS, name)
            self.assertEqual(tool.schema()['function']['parameters']['required'], list(tool.required))
            self.assertTrue(set(tool.required) <= set(tool.params), name)
        self.assertEqual(len(tools), len({t.name for t in tools.values()}))

    def test_disabling_shell_and_extraction_removes_those_tools(self):
        from agentharness.tools import build_tools
        full, bare = build_tools(), build_tools(allow_shell=False, allow_extract=False)
        self.assertLessEqual({'run_command', 'dev_start', 'extract_text', 'extract_table'}, set(full))
        self.assertFalse({'run_command', 'dev_start', 'extract_text', 'extract_table'} & set(bare))
