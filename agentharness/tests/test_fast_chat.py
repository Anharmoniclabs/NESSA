"""Stream assembly and single-loop small-chat/large-work routing."""
import io
import json
import unittest
from unittest.mock import patch

from agentharness.agent import Agent, AgentConfig
from agentharness.llm import ChatClient, ModelError
from agentharness.tests.test_agent import Base, ScriptedClient, BUGGY


def stream(*chunks, done=True):
    lines = [b'data: '+json.dumps(c).encode()+b'\n\n' for c in chunks]
    if done:
        lines.append(b'data: [DONE]\n\n')
    return io.BytesIO(b''.join(lines))


def chunk(delta, finish=None):
    return {'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}


class Streaming(unittest.TestCase):
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
