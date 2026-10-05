"""Promotion gate: frozen evaluations, isolated scoring, policy and explicit confirmation."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from agentharness import promote as pr
from agentharness.__main__ import main

PY = f'"{sys.executable}"'
# Task passes when candidate/solve.py prints the task's expected answer.
SCORER = ("import os,subprocess,sys,pathlib;"
          "want=pathlib.Path(os.environ['NESSA_TASK_DIR'],'expected.txt').read_text().strip();"
          "got=subprocess.run([sys.executable,'solve.py',os.environ['NESSA_TASK_ID']],capture_output=True,text=True).stdout.strip();"
          "sys.exit(0 if got==want else 1)")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scorer = self.tmp / 'score.py'
        self.scorer.write_text(SCORER)
        self.tasks = {}
        for name, answer in (('a', '1'), ('b', '2'), ('c', '3')):
            folder = self.tmp / 'tasks' / name
            folder.mkdir(parents=True)
            (folder / 'expected.txt').write_text(answer)
            self.tasks[name] = folder
        self.frozen = pr.freeze('demo', f'{PY} {self.scorer}', self.tasks, [self.scorer], timeout=30)

    def tree(self, name, answers):
        folder = self.tmp / name
        folder.mkdir()
        (folder / 'solve.py').write_text(f'import sys\nprint({answers!r}.get(sys.argv[1], "?"))\n')
        return folder

    def card(self, name, answers):
        return pr.evaluate(self.frozen, self.tree(name, answers), name)


class Scoring(Fixture):
    def test_scores_each_task_by_exit_code_and_records_identity(self):
        card = self.card('base', {'a': '1', 'b': '9'})
        self.assertEqual(card.passed, {'a'})
        self.assertEqual(set(card.tasks), {'a', 'b', 'c'})
        self.assertEqual(len(card.identity), 64)
        self.assertGreater(card.peak_rss_mb, 0)

    def test_candidate_cannot_edit_the_exam_or_leak_files_between_runs(self):
        tree = self.tree('cheat', {})
        (tree / 'solve.py').write_text(
            "import os,pathlib,sys\n"
            "pathlib.Path(os.environ['NESSA_TASK_DIR'],'expected.txt').write_text('!')\n"
            "pathlib.Path('leak.txt').write_text('x')\n"
            "print('!')\n")
        card = pr.evaluate(self.frozen, tree, 'cheat')
        self.assertEqual(card.passed, set(), 'editing the task copy must not make a task pass')
        self.assertEqual((self.tasks['a'] / 'expected.txt').read_text(), '1')
        self.assertFalse((tree / 'leak.txt').exists(), 'scoring runs in a copy')

    def test_a_changed_task_or_scorer_invalidates_the_evaluation(self):
        (self.tasks['a'] / 'expected.txt').write_text('changed')
        with self.assertRaisesRegex(pr.PromotionError, "task 'a' changed"):
            pr.evaluate(self.frozen, self.tree('t', {}), 't')
        (self.tasks['a'] / 'expected.txt').write_text('1')
        self.scorer.write_text(SCORER + '\n# edited')
        with self.assertRaisesRegex(pr.PromotionError, 'scoring command inputs changed'):
            pr.evaluate(self.frozen, self.tree('t2', {}), 't2')

    def test_timeout_fails_the_task_instead_of_hanging(self):
        frozen = pr.freeze('slow', f'{PY} -c "import time; time.sleep(5)"', {'a': self.tasks['a']}, timeout=0.3)
        card = pr.evaluate(frozen, self.tree('s', {}), 's')
        self.assertFalse(card.tasks['a'].passed)
        self.assertIn('timeout', card.tasks['a'].detail)

    def test_repeats_require_every_run_to_pass(self):
        flaky = self.tmp / 'flaky.py'
        flaky.write_text("import pathlib,sys\nm=pathlib.Path(sys.argv[1]);n=int(m.read_text() or 0) if m.exists() else 0\n"
                         "m.write_text(str(n+1));sys.exit(0 if n==0 else 1)\n")
        marker = self.tmp / 'count.txt'
        frozen = pr.freeze('flaky', f'{PY} {flaky} {marker}', {'a': self.tasks['a']}, [flaky], repeats=2)
        self.assertFalse(pr.evaluate(frozen, self.tree('f', {}), 'f').tasks['a'].passed)

    def test_memory_is_measured_per_evaluation_not_carried_over(self):
        hog = self.tmp / 'hog.py'
        hog.write_text("x=bytearray(150*1024*1024)\nx[::4096]=b'1'*len(x[::4096])\n")
        big = pr.evaluate(pr.freeze('big', f'{PY} {hog}', {'a': self.tasks['a']}, [hog]), self.tree('m1', {}), 'big')
        small = pr.evaluate(pr.freeze('small', f'{PY} -c pass', {'a': self.tasks['a']}), self.tree('m2', {}), 'small')
        self.assertGreater(big.peak_rss_mb, 100)
        self.assertLess(small.peak_rss_mb, 60)

    def test_freeze_rejects_empty_or_invalid_input(self):
        with self.assertRaises(pr.PromotionError):
            pr.freeze('x', 'true', {})
        with self.assertRaises(pr.PromotionError):
            pr.freeze('x', ' ', self.tasks)
        with self.assertRaises(pr.PromotionError):
            pr.freeze('x', 'true', {'a': self.tmp / 'missing'})


class Gate(Fixture):
    def test_improvement_without_regression_passes_policy(self):
        decision = pr.decide(self.card('base', {'a': '1'}), self.card('cand', {'a': '1', 'b': '2'}))
        self.assertTrue(decision.promote, decision.reasons)
        self.assertEqual(decision.improvements, ['b'])

    def test_any_regression_blocks_even_with_more_improvements(self):
        decision = pr.decide(self.card('base', {'a': '1'}), self.card('cand', {'b': '2', 'c': '3'}))
        self.assertFalse(decision.promote)
        self.assertEqual(decision.regressions, ['a'])
        self.assertTrue(any('regression' in r for r in decision.reasons))

    def test_no_improvement_and_identical_trees_do_not_promote(self):
        same = self.card('base', {'a': '1'})
        decision = pr.decide(same, self.card('cand2', {'a': '1'}))
        self.assertFalse(decision.promote)
        self.assertTrue(any('improvement' in r for r in decision.reasons))

    def test_slowdown_and_memory_growth_are_gated(self):
        base, cand = self.card('base', {'a': '1'}), self.card('cand', {'a': '1', 'b': '2'})
        slow = pr.Scorecard.from_json(cand.to_json())
        for score in slow.tasks.values():
            score.seconds = max(base.median_seconds, 0.01) * 10
        slow.peak_rss_mb = base.peak_rss_mb * 3
        decision = pr.decide(base, slow)
        self.assertFalse(decision.promote)
        self.assertTrue(any('median time' in r for r in decision.reasons))
        self.assertTrue(any('peak memory' in r for r in decision.reasons))
        self.assertTrue(pr.decide(base, slow, pr.Policy(max_slowdown=1000, max_memory_growth=1000)).promote)

    def test_scorecards_from_different_evaluations_cannot_be_compared(self):
        other = pr.freeze('other', f'{PY} {self.scorer}', self.tasks, [self.scorer], timeout=31)
        base = self.card('base', {'a': '1'})
        cand = pr.evaluate(other, self.tree('c', {'a': '1', 'b': '2'}), 'c')
        with self.assertRaisesRegex(pr.PromotionError, 'different evaluations'):
            pr.decide(base, cand)

    def test_promotion_needs_the_candidate_identity_echoed_back(self):
        base, cand = self.card('base', {'a': '1'}), self.card('cand', {'a': '1', 'b': '2'})
        record = self.tmp / 'promotion.json'
        with self.assertRaisesRegex(pr.PromotionError, 'confirmation'):
            pr.promote(base, cand, 'wrong', record)
        self.assertFalse(record.exists())
        pr.promote(base, cand, cand.identity[:12], record)
        saved = json.loads(record.read_text())
        self.assertEqual(saved['candidate']['identity'], cand.identity)
        self.assertEqual(saved['baseline']['identity'], base.identity)
        self.assertTrue(saved['decision']['promote'])

    def test_a_failing_policy_cannot_be_confirmed_into_a_promotion(self):
        base, cand = self.card('base', {'a': '1'}), self.card('cand', {'b': '2'})
        with self.assertRaisesRegex(pr.PromotionError, 'not promoted'):
            pr.promote(base, cand, cand.identity[:12], self.tmp / 'p.json')


class CommandLine(Fixture):
    def test_freeze_evaluate_decide_confirm_end_to_end(self):
        frozen, out = self.tmp / 'frozen.json', self.tmp
        self.assertEqual(main(['promote', 'freeze', '--name', 'demo', '--command', f'{PY} {self.scorer}',
                               '--scorer', str(self.scorer), '--out', str(frozen),
                               *sum((['--task', f'{k}={v}'] for k, v in self.tasks.items()), [])]), 0)
        base_tree, cand_tree = self.tree('base', {'a': '1'}), self.tree('cand', {'a': '1', 'b': '2'})
        for label, tree in (('baseline', base_tree), ('candidate', cand_tree)):
            self.assertEqual(main(['promote', 'evaluate', str(frozen), str(tree), '--label', label,
                                   '--out', str(out / f'{label}.json')]), 0)
        baseline, candidate = str(out / 'baseline.json'), str(out / 'candidate.json')
        self.assertEqual(main(['promote', 'decide', baseline, candidate]), 0)
        record = out / 'promotion.json'
        self.assertEqual(main(['promote', 'decide', baseline, candidate, '--confirm', 'nope',
                               '--record', str(record)]), 1)
        identity = pr.load_scorecard(Path(candidate)).identity[:12]
        self.assertEqual(main(['promote', 'decide', baseline, candidate, '--confirm', identity,
                               '--record', str(record)]), 0)
        self.assertTrue(record.exists())

    def test_tampering_after_freeze_is_a_clean_cli_error(self):
        frozen = self.tmp / 'f.json'
        frozen.write_text(self.frozen.to_json())
        (self.tasks['b'] / 'expected.txt').write_text('tampered')
        self.assertEqual(main(['promote', 'evaluate', str(frozen), str(self.tree('x', {})), '--label', 'x',
                               '--out', str(self.tmp / 'x.json')]), 1)

    def test_malformed_task_argument_is_rejected(self):
        self.assertEqual(main(['promote', 'freeze', '--name', 'n', '--command', 'true', '--task', 'no-equals',
                               '--out', str(self.tmp / 'f.json')]), 1)


if __name__ == '__main__':
    unittest.main()
