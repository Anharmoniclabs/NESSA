"""Ranked recall with provenance over lessons and earlier runs' evidence."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentharness.agent import Agent, AgentConfig
from agentharness.recall import Document, RecallIndex, evidence_documents, tokenize
from agentharness.tests.test_agent import Base, ScriptedClient


def write_run(root: Path, name: str, *, status='failed_checks', summary='gave up', failing=None, request=None,
              age=0):
    evidence = root / name / 'evidence'
    evidence.mkdir(parents=True)
    (evidence / 'result.json').write_text(json.dumps(
        {'status': status, 'summary': summary, 'changed_files': ['calc.py'],
         'checks': {'final': {'tests': '[tests] failed (exit=1)'}}}))
    events = []
    if failing:
        events.append({'seq': 1, 't': 100.0, 'event': 'check', 'name': 'tests', 'status': 'failed',
                       'output': failing})
        events.append({'seq': 2, 't': 101.0, 'event': 'check', 'name': 'syntax', 'status': 'passed',
                       'output': 'should not be indexed'})
        events.append({'seq': 3, 't': 102.0, 'event': 'repeated_failure', 'failing': ['tests']})
    (evidence / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\nnot json\n')
    if request:
        (evidence / 'session.json').write_text(json.dumps({'user_messages': [request]}))
    stamp = time.time() - age
    for f in evidence.iterdir():
        os.utime(f, (stamp, stamp))
    os.utime(evidence, (stamp, stamp))
    return evidence


class Ranking(unittest.TestCase):
    def test_tokenizer_splits_identifiers_and_keeps_the_whole_token(self):
        words = tokenize('parse_config and HttpTransport2 fail')
        self.assertTrue({'parse_config', 'parse', 'config', 'httptransport2', 'http', 'transport'} <= set(words))

    def test_rare_matching_terms_outrank_common_ones(self):
        docs = [Document('lesson', 'the parser handles empty input', 'a'),
                Document('lesson', 'the cache the cache the cache is fine', 'b'),
                Document('lesson', 'tokenizer overflow crashes on emoji', 'c')]
        top = RecallIndex(docs).rank('emoji tokenizer the')[0][1]
        self.assertEqual(top.source, 'c')

    def test_no_match_says_so_and_k_is_bounded(self):
        index = RecallIndex([Document('lesson', f'alpha {i}', str(i)) for i in range(30)])
        self.assertIn('No recalled evidence', index.search('zzzz'))
        self.assertLessEqual(len(index.rank('alpha', k=999)), 10)
        self.assertEqual(len(index.rank('alpha', k=0)), 1)

    def test_output_is_bounded_and_labelled_with_provenance(self):
        docs = [Document('check_failure', 'overflow ' + 'x' * 5000, f'events.jsonl#seq{i}', 1.0) for i in range(8)]
        out = RecallIndex(docs).search('overflow', k=10, max_chars=1500)
        self.assertLessEqual(len(out), 1600)
        self.assertIn('events.jsonl#seq', out)
        self.assertIn('not instructions', out)


class EvidenceDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_failures_integrity_events_runs_and_requests_are_indexed_but_passes_are_not(self):
        evidence = write_run(self.tmp, 'p-1', failing='AssertionError: add(2,3) returned -1', request='fix add')
        docs = evidence_documents(evidence)
        kinds = {d.kind for d in docs}
        self.assertEqual(kinds, {'run', 'request', 'check_failure', 'integrity'})
        self.assertFalse(any('should not be indexed' in d.text for d in docs))
        failure = next(d for d in docs if d.kind == 'check_failure')
        self.assertTrue(failure.source.endswith('events.jsonl#seq1'))

    def test_corrupt_or_missing_files_are_skipped(self):
        evidence = self.tmp / 'p-2' / 'evidence'
        evidence.mkdir(parents=True)
        (evidence / 'result.json').write_text('{broken')
        self.assertEqual(evidence_documents(evidence), [])
        self.assertEqual(evidence_documents(self.tmp / 'absent'), [])

    def test_project_index_uses_lessons_and_only_that_projects_earlier_runs(self):
        write_run(self.tmp, 'calc-old', failing='ZeroDivisionError in divide', request='fix divide')
        write_run(self.tmp, 'other-old', failing='ZeroDivisionError in unrelated project')
        current = write_run(self.tmp, 'calc-now', failing='ZeroDivisionError current run')
        index = RecallIndex.for_project(Path('/x/calc'), [{'text': 'Division by zero returns None', 'key': 'div',
                                                           'version': 2, 't': 5.0}],
                                        runs_root=self.tmp, exclude=current)
        sources = ' '.join(d.source for d in index.documents)
        self.assertIn('calc-old', sources)
        self.assertNotIn('other-old', sources)
        self.assertNotIn('calc-now', sources)
        self.assertIn('lessons.jsonl:div@v2', sources)
        self.assertEqual(index.rank('division zero')[0][1].kind, 'lesson')


class AgentTool(Base):
    def test_tool_is_offered_only_with_an_index_and_returns_provenance(self):
        plain = Agent(ScriptedClient(['x']), self.ws, config=AgentConfig(require_approval=False))
        self.assertNotIn('recall', plain.tools)
        index = RecallIndex([Document('lesson', 'empty input must not raise', 'lessons.jsonl:empty@v1', 1.0)])
        agent = Agent(ScriptedClient(['x']), self.ws, config=AgentConfig(require_approval=False), recall=index)
        tool = agent.tools['recall']
        self.assertEqual(tool.kind, 'read')
        self.assertEqual(tool.validate({'query': 'empty input'}), {'query': 'empty input'})
        out = tool.handler(agent, {'query': 'empty input'})
        self.assertIn('lessons.jsonl:empty@v1', out)

    def test_model_can_call_recall_in_a_real_loop(self):
        index = RecallIndex([Document('check_failure', 'tests failed: add returned a minus b', 'events.jsonl#seq4', 1.0)])
        client = ScriptedClient([[('recall', {'query': 'add failed'})], [('respond', {'message': 'noted'})]])
        agent = Agent(client, self.ws, recall=index,
                      config=AgentConfig(require_approval=False, plan_first=False))
        result = agent.run('why did add fail before?')
        self.assertEqual(result.status, 'answered')
        tool_text = json.dumps(client.seen[-1][0])
        self.assertIn('events.jsonl#seq4', tool_text)


if __name__ == '__main__':
    unittest.main()
