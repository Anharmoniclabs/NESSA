import json
from pathlib import Path
from unittest.mock import patch

from agentharness.agent import Agent, AgentConfig
from agentharness.llm import ChatClient, Reply
from agentharness.tests.test_agent import Base, ScriptedClient
from agentharness.tools import _local_path
from agentharness.workspace import ToolError


class AssistantTools(Base):
    def test_news_answer_uses_article_evidence_not_generic_model_summary(self):
        data={'kind':'news','effective_query':'artificial intelligence','window_days':7,
              'retrieved':'2026-10-04T12:00:00+00:00','results':[{
                  'title':'New AI model released','url':'https://example.com/ai-release',
                  'publisher':'example.com','published':'2026-10-04T10:00:00+00:00',
                  'page_status':'fetched','page_excerpt':'A new AI model was released today.'}]}
        fast=ScriptedClient([[('web_search',{'query':'news'})], 'Fox and CNN offer news.'])
        agent=Agent(fast,self.ws,config=AgentConfig(conversational=True,require_approval=False))
        with patch('agentharness.online.web_search',return_value=json.dumps(data)) as search:
            result=agent.run('search the web for the latest news in ai')
        self.assertEqual(search.call_args.kwargs['request'],'search the web for the latest news in ai')
        self.assertIn('New AI model released',result.summary)
        self.assertIn('2026-10-04',result.summary)
        self.assertNotIn('Fox and CNN',result.summary)

    def test_news_without_dated_articles_cannot_complete(self):
        fast=ScriptedClient([[('web_search',{'query':'news'})], 'Latest news is on Fox News.'])
        agent=Agent(fast,self.ws,config=AgentConfig(conversational=True,require_approval=False))
        with patch('agentharness.online.web_search',return_value=json.dumps({'results':[{
                'title':'Fox News','url':'https://www.foxnews.com/'}]})):
            result=agent.run('latest news in AI')
        self.assertEqual(result.status,'blocked')
        self.assertNotIn('Latest news is on Fox',result.summary)

    def test_weather_tool_executes_in_chat_and_records_evidence(self):
        fast = ScriptedClient([[('weather', {'location':'NYC'})], 'New York: 20 C. Source: https://open-meteo.com'])
        agent = Agent(ScriptedClient(['unused']), self.ws,
                      config=AgentConfig(conversational=True, require_approval=False, tool_mode='text'), chat_client=fast)
        with patch('agentharness.online.weather', return_value='New York 20 C source https://open-meteo.com') as weather:
            result = agent.run('whats todays weather in nyc')
        weather.assert_called_once_with('NYC')
        self.assertEqual(result.status, 'answered')
        self.assertIn('weather', fast.seen[0][1])
        self.assertIn('web_search', fast.seen[0][1])
        self.assertIn('run_command', fast.seen[0][1])
        self.assertIn('operation_finished', (self.ws.work_dir/'evidence/events.jsonl').read_text())

    def test_tool_failure_is_returned_to_model(self):
        fast = ScriptedClient([[('web_search', {'query':'today news'})], 'Search failed.'])
        agent = Agent(fast, self.ws, config=AgentConfig(conversational=True, require_approval=False))
        with patch('agentharness.online.web_search', side_effect=ToolError('provider unavailable')):
            result = agent.run('Search today news')
        self.assertEqual(result.status, 'blocked')
        self.assertIn('provider unavailable', str(fast.seen[-1][0]))

    def test_local_read_and_path_boundary(self):
        agent = Agent(ScriptedClient(['unused']), self.ws,
                      config=AgentConfig(require_approval=False, local_roots=(str(self.proj),)))
        tool = agent.tools['local_read']
        self.assertIn('def add', tool.handler(agent, {'path':str(self.proj/'calc.py')}))
        with self.assertRaises(ToolError): _local_path(agent, str(self.proj.parent/'secret.txt'))
        (self.proj/'escape').symlink_to(self.proj.parent)
        with self.assertRaises(ToolError): _local_path(agent, str(self.proj/'escape'/'secret.txt'))

    def test_local_regex_extract(self):
        path = self.proj/'invoice.txt'
        path.write_text('Invoice 12345 Total $42.50')
        agent = Agent(ScriptedClient(['unused']), self.ws,
                      config=AgentConfig(require_approval=False, local_roots=(str(self.proj),)))
        result = agent.tools['local_extract'].handler(agent, {'paths':[str(path)],
            'fields':[{'name':'total', 'pattern':r'\$(\d+\.\d+)', 'type':'float'}]})
        self.assertEqual(json.loads(result)['records'][0]['total'],42.5)

    def test_direct_command_requires_approval(self):
        fast = ScriptedClient([[('run_command', {'command':'python --version'})], 'Not approved.'])
        agent = Agent(fast, self.ws, config=AgentConfig(conversational=True),
                      approver=lambda plan:(False, 'User declined'))
        with patch('agentharness.tools.run_command') as run:
            result = agent.run('Run python --version')
        run.assert_not_called()
        self.assertEqual(result.status,'answered')

    def test_direct_command_runs_after_approval(self):
        fast = ScriptedClient([[('run_command', {'command':'python --version'})], 'Python checked.'])
        approvals=[]
        def approve(plan):
            approvals.append(plan)
            return True,''
        agent = Agent(fast, self.ws, config=AgentConfig(conversational=True), approver=approve)
        result=agent.run('Run python --version')
        self.assertEqual(approvals[0]['steps'],['python --version'])
        self.assertEqual(result.status,'answered')
        self.assertIn('exit=0',(self.ws.work_dir/'evidence/events.jsonl').read_text())

    def test_shell_handoff_still_requires_workflow(self):
        fast = ScriptedClient([[('start_work', {'request':'Run a Python version check'})]])
        worker = ScriptedClient([[('propose_plan', {'goal':'Check Python','steps':['Run version']})],
            [('run_command', {'command':'python --version'})], [('respond', {'message':'Python checked.'})]])
        agent = Agent(worker, self.ws, config=AgentConfig(conversational=True, require_approval=False,
                      baseline_checks=False), chat_client=fast)
        result = agent.run('Run python --version')
        self.assertEqual(result.status, 'answered')
        events=(self.ws.work_dir/'evidence/events.jsonl').read_text()
        self.assertIn('exit=0', events)
