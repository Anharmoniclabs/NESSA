"""Stream assembly and single-loop small-chat/large-work routing."""
import io
import json
import unittest
from unittest.mock import patch

from agentharness.agent import Agent, AgentConfig
from agentharness.llm import ChatClient, ModelError, PartialResponse, Reply
from agentharness.tests.test_agent import Base, ScriptedClient, BUGGY


def stream(*chunks, done=True):
    lines = [b'data: '+json.dumps(c).encode()+b'\n\n' for c in chunks]
    if done:
        lines.append(b'data: [DONE]\n\n')
    return io.BytesIO(b''.join(lines))


def chunk(delta, finish=None):
    return {'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}


class Streaming(unittest.TestCase):
    def test_inline_reasoning_split_across_chunks_is_never_visible(self):
        seen = []
        client = ChatClient('http://localhost:11435/v1', 'nessa-lfm:latest', on_delta=seen.append)
        response = stream(chunk({'content': '<thi'}), chunk({'content': 'nk>hidden'}),
                          chunk({'content': '</thi'}), chunk({'content': 'nk>Answer.'}, 'stop'))
        with patch.object(client._opener, 'open', return_value=response):
            reply = client.chat([])
        self.assertEqual(''.join(seen), 'Answer.')
        self.assertEqual(reply.content, 'Answer.')

    def test_interrupted_thinking_is_not_saved_as_prose(self):
        seen = []
        client = ChatClient('http://localhost:11435/v1', 'nessa-lfm:latest', on_delta=seen.append)
        with patch.object(client._opener, 'open', return_value=stream(chunk({'content': '<think>unfinished'}), done=False)):
            with self.assertRaises(ModelError) as caught:
                client.chat([])
        self.assertNotIsInstance(caught.exception, PartialResponse)
        self.assertEqual(seen, [])

    def test_deadline_preserves_text_without_replaying(self):
        client = ChatClient('http://localhost:11436/v1', 'small', timeout=5, on_delta=lambda _: None)
        with patch.object(client._opener, 'open', return_value=stream(chunk({'content': 'The knight'}))), \
                patch('agentharness.llm.time.monotonic', side_effect=[0, 1, 6]):
            with self.assertRaises(PartialResponse) as caught:
                client.chat([])
        self.assertEqual(caught.exception.content, 'The knight')

    def test_partial_tool_call_never_becomes_resumable_prose(self):
        client = ChatClient('http://localhost:11436/v1', 'small', on_delta=lambda _: None)
        response = stream(chunk({'content': 'Working', 'tool_calls': [
            {'index': 0, 'function': {'name': 'run_command', 'arguments': '{'}}]}), done=False)
        with patch.object(client._opener, 'open', return_value=response):
            with self.assertRaises(ModelError) as caught:
                client.chat([])
        self.assertNotIsInstance(caught.exception, PartialResponse)

    def test_tokens_arrive_before_complete_and_thoughts_are_not_shown(self):
        seen=[]
        client=ChatClient('http://localhost:11436/v1','small',on_delta=seen.append)
        response=stream(chunk({'reasoning_content':'private'}),chunk({'content':'Hello '}),
                        chunk({'content':'there'},'stop'),{'choices':[],'usage':{'prompt_tokens':42}})
        with patch.object(client._opener,'open',return_value=response) as request:
            reply=client.chat([{'role':'user','content':'hey'}])
        self.assertEqual(seen,['Hello ','there'])
        self.assertEqual(reply.content,'Hello there')
        self.assertEqual(reply.prompt_tokens,42)
        self.assertEqual(reply.reasoning,'private')
        self.assertTrue(json.loads(request.call_args.args[0].data)['stream'])

    def test_fragmented_native_handoff_is_reassembled(self):
        client=ChatClient('http://localhost:11436/v1','small',on_delta=lambda _:None)
        response=stream(chunk({'tool_calls':[{'index':0,'id':'c0','function':{'name':'start_', 'arguments':'{"request":'}}]}),
                        chunk({'tool_calls':[{'index':0,'function':{'name':'work','arguments':'"Fix add"}'}}]},'tool_calls'))
        with patch.object(client._opener,'open',return_value=response):
            reply=client.chat([{'role':'user','content':'Fix add'}])
        self.assertTrue(reply.native)
        self.assertEqual(reply.tool_calls[0].name,'start_work')
        self.assertEqual(reply.tool_calls[0].arguments,{'request':'Fix add'})

    def test_incomplete_stream_is_not_success_or_retried(self):
        client=ChatClient('http://localhost:11436/v1','small',on_delta=lambda _:None)
        with patch.object(client._opener,'open',return_value=stream(chunk({'content':'partial'}),done=False)) as request:
            with self.assertRaises(ModelError):client.chat([])
        self.assertEqual(request.call_count,1)

    def test_stop_interrupts_stream(self):
        def stop(_):raise KeyboardInterrupt
        client=ChatClient('http://localhost:11436/v1','small',on_delta=stop)
        response=stream(chunk({'content':'Hello'}),chunk({'content':' later'},'stop'))
        with patch.object(client._opener,'open',return_value=response):
            with self.assertRaises(KeyboardInterrupt):client.chat([])
        self.assertTrue(response.closed)


class Routing(Base):
    def make_chat(self, fast, **options):
        return Agent(ScriptedClient(['unused']), self.ws, chat_client=fast,
                     config=AgentConfig(conversational=True, require_approval=False, **options))

    def test_multiple_saved_passes_finish_without_user_reprompt(self):
        fast = ScriptedClient(['Castle on an abandoned island. [[CONTINUE]]', 'Chapter 1: Arrival.'])
        agent = self.make_chat(fast)
        result = agent.run('Outline a knight romance')
        self.assertEqual(result.status, 'answered')
        self.assertEqual(result.summary, 'Castle on an abandoned island.\n\nChapter 1: Arrival.')
        self.assertIn('abandoned island', json.dumps(fast.seen[-1]))
        self.assertFalse(agent.store.load()['chat_progress']['pending'])
        events = [json.loads(line) for line in agent.log.path.read_text().splitlines()]
        self.assertEqual(len([e for e in events if e['event'] == 'chat_part']), 2)

    def test_length_limit_and_timeout_resume_from_saved_text(self):
        fast = ScriptedClient(['unused'])
        with patch.object(fast, 'chat', side_effect=[
                Reply('The knight arrived', [], False, finish_reason='length'),
                PartialResponse('timeout', ' at the castle.'),
                Reply('He met the princess.', [], False, finish_reason='stop')]):
            result = self.make_chat(fast).run('Write the island romance')
        self.assertEqual(result.status, 'answered')
        self.assertIn('The knight arrived', result.summary)
        self.assertIn('at the castle.', result.summary)

    def test_pass_cap_persists_cursor_across_new_agent(self):
        first = self.make_chat(ScriptedClient(['A knight named Rowan. [[CONTINUE]]']), chat_passes=1)
        result = first.run('Romance on an abandoned island')
        self.assertEqual(result.status, 'awaiting_continuation')
        fast = ScriptedClient(['Rowan entered the castle.'])
        result = self.make_chat(fast).run('', resume=True, message='continue')
        self.assertEqual(result.status, 'answered')
        self.assertIn('Rowan', json.dumps(fast.seen[0]))
        self.assertIn('abandoned island', json.dumps(fast.seen[0]))

    def test_stop_preserves_partial_in_session_without_auto_continuing(self):
        fast = ScriptedClient(['unused'])
        stopped = KeyboardInterrupt()
        stopped.partial_content = 'The dragon watched Rowan'
        agent = self.make_chat(fast)
        with patch.object(fast, 'chat', side_effect=stopped) as request:
            result = agent.run('Write a romance')
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(request.call_count, 1)
        saved = agent.store.load()
        self.assertEqual(saved['messages'][-1]['content'], stopped.partial_content)
        self.assertTrue(saved['chat_progress']['pending'])

    def test_repeated_section_pauses_instead_of_looping(self):
        fast = ScriptedClient(['Same section [[CONTINUE]]'])
        result = self.make_chat(fast).run('Write a romance')
        self.assertEqual(result.status, 'awaiting_continuation')
        self.assertEqual(result.summary.count('Same section'), 1)
        self.assertEqual(len(fast.seen), 2)

    def test_compaction_keeps_early_premise_and_latest_cursor(self):
        agent = self.make_chat(ScriptedClient(['unused']), max_context_chars=11000)
        agent.task = 'continue'
        agent.user_messages = ['romance', 'a knight', 'castle on an abandoned island'] + ['continue'] * 100
        agent.chat_anchors = ['Rowan loves Mira; the dragon guards a curse.']
        agent.chat_progress = dict(request='Full chapter outline', tail='Chapter 7: The escape', pending=True)
        agent.messages = [{'role': 'system', 'content': agent._system()}] + [
            {'role': 'assistant', 'content': 'long draft ' * 200} for _ in range(30)]
        agent._compact()
        payload = json.dumps(agent.messages)
        self.assertLess(len(payload), 11000)
        for fact in ('abandoned island', 'Rowan loves Mira', 'Chapter 7', 'Full chapter outline'):
            self.assertIn(fact, payload)

    def test_writing_followups_stay_in_chat_and_preserve_context(self):
        fast = ScriptedClient(['What genre?', 'Here is a fantasy outline.', 'Chapter One: The bell rang.'])
        large = ScriptedClient(['Should not run'])
        cfg = AgentConfig(conversational=True)
        prompts = ['write me a book', 'fantasy about a lost bell', 'write chapter one']
        for index, prompt in enumerate(prompts):
            result = Agent(large, self.ws, config=cfg, chat_client=fast,
                           approver=lambda _: self.fail('Chat writing must not request approval')).run(
                prompts[0], resume=index > 0, message=prompt if index else '')
            self.assertEqual(result.status, 'answered')
        self.assertEqual(large.seen, [])
        self.assertIn('lost bell', json.dumps(fast.seen[-1][0]))
        self.assertIn('write me a book', json.dumps(fast.seen[-1][0]))
        self.assertIsNone(result.plan)
        self.assertEqual((self.ws.repo/'calc.py').read_text(), BUGGY)

    def test_query_memory_refreshes_after_resume_without_stale_duplicate(self):
        cfg = AgentConfig(conversational=True, require_approval=False)
        first = ScriptedClient(['Got it.'])
        Agent(first, self.ws, config=cfg, conversation_context='Old title: Amber.').run('Hello')
        fast = ScriptedClient(['Your title is Blue Moon.'])
        result = Agent(fast, self.ws, config=cfg,
                       conversation_context='Corrected title: Blue Moon.').run('', resume=True, message='What title?')
        self.assertEqual(result.status, 'answered')
        memory = [m for m in fast.seen[0][0] if m.get('content', '').startswith('[query memory]')]
        self.assertEqual(len(memory), 1)
        self.assertIn('Blue Moon', memory[0]['content'])
        self.assertNotIn('Amber', memory[0]['content'])

    def test_retrieved_memory_survives_compaction(self):
        agent = self.make_chat(ScriptedClient(['unused']), max_context_chars=11000)
        agent.conversation_context = 'Remember: the submarine is called Marigold.'
        agent.task = 'What did we call it?'
        agent.messages = [dict(role='system', content=agent._system())] + [
            dict(role='assistant', content='long discussion ' * 200) for _ in range(30)]
        agent._compact()
        self.assertIn('Marigold', json.dumps(agent.messages))

    def test_greeting_uses_only_small_model(self):
        fast=ScriptedClient(['Hello!'])
        large=ScriptedClient(['Should not run'])
        cfg=AgentConfig(conversational=True,require_approval=False)
        result=Agent(large,self.ws,config=cfg,chat_client=fast).run('hey')
        self.assertEqual(result.status,'answered')
        self.assertEqual(len(fast.seen),1)
        self.assertEqual(large.seen,[])

    def test_handoff_preserves_user_scope_and_approval_then_returns_to_chat(self):
        fast=ScriptedClient([[('start_work',{'request':'Inspect calc.py'})],'You are welcome.'])
        large=ScriptedClient([[('propose_plan',{'goal':'Fix add','steps':['Edit calc.py']})]])
        large.model='nessa-lfm:latest'
        approvals=[]
        def reject(plan):approvals.append(plan);return False,''
        cfg=AgentConfig(conversational=True)
        task='Fix add but do not touch tests.'
        result=Agent(large,self.ws,config=cfg,chat_client=fast,approver=reject).run(task)
        self.assertEqual(result.status,'rejected')
        self.assertEqual(len(approvals),1)
        self.assertIn(task,json.dumps(large.seen[0][0]))
        self.assertNotIn('LFM native call envelope',fast.seen[0][0][0]['content'])
        self.assertIn('LFM native call envelope',large.seen[0][0][0]['content'])
        self.assertEqual((self.ws.repo/'calc.py').read_text(),BUGGY)
        result=Agent(large,self.ws,config=cfg,chat_client=fast,approver=reject).run('',resume=True,message='Thanks')
        self.assertEqual(result.status,'answered')
        self.assertEqual(len(large.seen),1)
        self.assertIn(task,json.dumps(fast.seen[-1][0]))
