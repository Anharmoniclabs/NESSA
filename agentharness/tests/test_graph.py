import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentharness.graph import GraphIndex
from agentharness.tools import build_tools
from agentharness.tests.test_agent import Base, ScriptedClient
from agentharness.agent import Agent, AgentConfig


class Graph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'project'; self.root.mkdir()
        self.g = GraphIndex(self.root,Path(self.tmp.name)/'graph.sqlite3')

    def test_hop_finds_dependency_without_query_words(self):
        (self.root/'entry.py').write_text('from engine import helper\ndef launch_orbit():\n    return helper()\n')
        (self.root/'engine.py').write_text('def helper():\n    return 42\n')
        rows = self.g.query('launch orbit')
        neighbor = next(r for r in rows if r['name']=='helper')
        self.assertIn('--calls-->',neighbor['via'])
        self.assertEqual(neighbor['path'],'engine.py')
        self.assertEqual(neighbor['start'],1)

    def test_refresh_removes_stale_and_changed_evidence(self):
        p = self.root/'sample.py'; p.write_text('def comet():\n    return 1\n')
        self.assertIn('return 1',self.g.retrieve('comet'))
        p.write_text('def comet():\n    return 2\n')
        self.assertNotIn('return 1',self.g.retrieve('comet'))
        self.assertIn('return 2',self.g.retrieve('comet'))
        p.unlink()
        self.assertEqual(self.g.retrieve('comet'),'')

    def test_unchanged_graph_skips_parsing(self):
        (self.root/'a.py').write_text('def meteor(): pass')
        self.g.refresh()
        with patch('agentharness.graph.ast.parse',side_effect=AssertionError('reparsed')):
            self.assertFalse(self.g.refresh()['changed'])

    def test_excludes_secrets_links_and_bounds_output(self):
        (self.root/'secret.txt').write_text('meteor confidential')
        (self.root/'.env').write_text('meteor credential')
        outside = Path(self.tmp.name)/'private.txt';outside.write_text('meteor outside')
        (self.root/'linked.txt').symlink_to(outside)
        (self.root/'a.md').write_text('# Meteor\n'+'A description. '*300)
        text=self.g.retrieve('meteor',max_chars=500)
        self.assertLessEqual(len(text),500)
        for word in ('confidential','credential','outside'):
            self.assertNotIn(word,text)
        tool=build_tools()['graph_search']
        self.assertEqual(tool.kind,'read')
        self.assertFalse(tool.cacheable)


class GraphWiring(Base):
    def test_chat_receives_cited_graph_evidence(self):
        (self.ws.repo/'orbit.py').write_text('def orbit():\n    return 42\n')
        client=ScriptedClient(['It returns 42.'])
        agent=Agent(client,self.ws,config=AgentConfig(conversational=True,require_approval=False))
        result=agent.run('Explain orbit')
        self.assertEqual(result.status,'answered')
        self.assertIn('[project graph evidence:',json.dumps(client.seen[0]))
        self.assertIn('orbit.py',json.dumps(client.seen[0]))
