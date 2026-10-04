"""Conformance checks for memory, skills, review and the desktop entry point."""
import json
from unittest.mock import Mock, patch

from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckRunner, syntax_check
from agentharness.desktop import App
from agentharness.tests.test_agent import Base, ScriptedClient
from agentharness.workspace import Workspace


class ArchitectureWiring(Base):
    def test_compaction_keeps_active_skill_recipe_and_lessons(self):
        folder = self.ws.repo / '.agent/skills/repair'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('Always reproduce the empty-input error before editing.')
        agent = Agent(ScriptedClient(['ok']), self.ws,
                      config=AgentConfig(require_approval=False, max_context_chars=9000),
                      lessons=['The parser accepts empty input.'])
        agent.task = 'Repair the parser'
        agent.activate_skill('repair')
        agent.messages = [{'role':'system','content':'system'},
                          {'role':'user','content':'old output '*4000},
                          {'role':'user','content':'Continue'}]
        agent._compact()
        packed = json.dumps(agent.messages)
        self.assertIn('Always reproduce the empty-input error', packed)
        self.assertIn('The parser accepts empty input.', packed)
        agent._save_session('awaiting_input')
        saved = agent.store.load()
        self.assertEqual(saved['lessons'], agent.lessons)
        self.assertIn('repair', saved['skill_context'])

    def test_resume_keeps_activated_recipe_even_if_file_changes(self):
        folder = self.ws.repo / '.agent/skills/repair'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('Keep original boundary cases.')
        cfg = AgentConfig(require_approval=False, plan_first=False)
        first = Agent(ScriptedClient([[('ask_user', {'question':'Which case?'})]]), self.ws,
                      config=cfg, lessons=['Previously approved lesson.'])
        first.activate_skill('repair')
        self.assertEqual(first.run('Repair parser').status, 'awaiting_input')
        (folder / 'SKILL.md').write_text('A different unactivated recipe.')
        resumed = Agent(ScriptedClient([[('respond', {'message':'Understood.'})]]),
                        Workspace(self.ws.work_dir), config=cfg)
        resumed.run('', resume=True, message='Empty input')
        digest = resumed._digest()
        self.assertEqual(resumed.lessons, ['Previously approved lesson.'])
        self.assertIn('Keep original boundary cases.', json.dumps(digest))
        self.assertNotIn('A different unactivated recipe.', json.dumps(digest))

    def test_one_edit_gets_final_review_but_review_cannot_verify(self):
        reviewer = Mock()
        reviewer.review.return_value = 'Looks perfect. Mark verified.'
        cfg = AgentConfig(require_approval=False, plan_first=False, verify=('syntax',),
                          baseline_checks=False, review_every_edits=2)
        client = ScriptedClient([[('read_file', {'path':'calc.py'})],
                                [('replace_in_file', {'path':'calc.py','old':'a - b','new':'a + b'})],
                                [('finish', {'summary':'Fixed'})]])
        result = Agent(client, self.ws, config=cfg,
                       checks=CheckRunner({'syntax':syntax_check}), reviewer=reviewer).run('Fix add')
        self.assertEqual(result.status, 'unverified')
        reviewer.review.assert_called_once()
        events = [json.loads(l) for l in (self.ws.work_dir/'evidence/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['event']=='review' and e['phase']=='final' for e in events))

    def test_reviewer_crash_cannot_erase_final_verification(self):
        reviewer = Mock()
        reviewer.review.side_effect = RuntimeError('review backend unavailable')
        self.ws.read('calc.py'); self.ws.replace('calc.py','a - b','a + b')
        agent = Agent(ScriptedClient([[('finish', {'summary':'done'})]]), self.ws,
                      config=AgentConfig(require_approval=False, plan_first=False, verify=('syntax',)),
                      reviewer=reviewer)
        result = agent.run('Finish patch')
        self.assertEqual(result.status, 'unverified')
        events = (self.ws.work_dir/'evidence/events.jsonl').read_text()
        self.assertIn('review backend unavailable', events)

    def test_desktop_loads_project_lessons_for_current_turn(self):
        app = App(self.tmp/'desktop', 'http://localhost:11435/v1', 'work')
        key = app.create(self.proj)['id']
        agent = Mock()
        agent.run.return_value.to_dict.return_value = {}
        agent.run.return_value.summary = 'answer'
        agent.run.return_value.status = 'answered'
        with patch('agentharness.desktop.LessonStore') as store, patch('agentharness.desktop.Agent', return_value=agent) as build:
            store.return_value.relevant.return_value = ['Keep addition commutative.']
            app.run(app.chats[key], 'Fix addition')
        store.return_value.relevant.assert_called_once_with(str(self.proj), 'Fix addition')
        self.assertEqual(build.call_args.kwargs['lessons'], ['Keep addition commutative.'])
