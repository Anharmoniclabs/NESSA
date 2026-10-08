import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agentharness import creative
from agentharness.tools import build_tools
from agentharness.workspace import Workspace, ToolError
from agentharness.skills import SkillRegistry
from agentharness.permissions import Permissions
from agentharness.tests.test_agent import Base, ScriptedClient
from agentharness.agent import Agent, AgentConfig
from agentharness.llm import ToolCall


class Creative(unittest.TestCase):
    def test_all_apps_and_skills_discoverable(self):
        self.assertEqual(len(creative.APPS),7)
        with tempfile.TemporaryDirectory() as tmp:
            skills=SkillRegistry(Path(tmp))
            self.assertIn('blender-animation',skills.names())
            self.assertIn('creative-delivery',skills.names())
        tools=build_tools()
        self.assertEqual(tools['creative_call'].kind,'mcp')
        self.assertEqual(tools['blender_run'].kind,'dev')
        self.assertFalse(Permissions('plan').check('creative_call','mcp',{})[0])

    def test_blender_failure_and_timeout_are_not_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'scene.py').write_text('raise RuntimeError()')
            ctx=SimpleNamespace(ws=Workspace(root/'work',repo=root))
            with patch.object(creative,'executable',return_value='/blender'), \
                 patch.object(creative,'run_command',return_value=(1,'script failed',False)) as run:
                output=creative.blender_run(ctx,{'script':'scene.py'})
                self.assertTrue(output.startswith('ERROR:'))
                result=json.loads(output.removeprefix('ERROR: '))
                self.assertEqual(result['status'],'failed')
                self.assertIn('--python-exit-code 1',run.call_args.args[0])
            with patch.object(creative,'executable',return_value='/blender'), \
                 patch.object(creative,'run_command',return_value=(None,'',True)):
                self.assertTrue(creative.blender_run(ctx,{'script':'scene.py'}).startswith('TIMEOUT:'))
            with patch.object(creative,'executable',return_value='/blender'):
                with self.assertRaises(ToolError): creative.blender_run(ctx,{'script':'../outside.py'})

    def test_catalog_paginates_and_call_rejects_unknown_tool(self):
        specs={str(i):('filmcraft',dict(name=f'tool{i}',inputSchema={})) for i in range(9)}
        fake=SimpleNamespace(discovered=specs)
        with patch.object(creative,'bus',return_value=fake):
            page=json.loads(creative.discover(None,{'app':'filmcraft'}))
            self.assertEqual(len(page['tools']),5)
            self.assertEqual(page['next_offset'],5)
            with self.assertRaises(ToolError):
                creative.call(None,{'app':'filmcraft','tool':'imaginary','arguments':{}})


class CreativePermissions(Base):
    def test_denied_creative_operation_never_calls_backend(self):
        agent=Agent(ScriptedClient([]),self.ws,
                    config=AgentConfig(require_approval=False,permission_mode='default'),
                    asker=lambda *args:(False,'no',False))
        with patch('agentharness.creative.bus') as backend:
            result=agent._dispatch(ToolCall('c','creative_call',dict(app='filmcraft',tool='command_run',arguments={})), ['creative_call'])
        self.assertIn('denied',result)
        backend.assert_not_called()

    def test_production_permission_exposes_actual_command(self):
        from agentharness.production import Production
        from unittest.mock import Mock
        p=Production(self.ws.repo)
        p.update('task',dict(id='preview',argv=['blender','--version'],outputs=['preview.png']))
        asker=Mock(return_value=(False,'no',False))
        agent=Agent(ScriptedClient([]),self.ws,
                    config=AgentConfig(require_approval=False,permission_mode='default'),asker=asker)
        with patch('agentharness.production.Production.run') as run:
            out=agent._dispatch(ToolCall('p','production_run',{'task':'preview'}),['production_run'])
        self.assertIn('denied',out)
        run.assert_not_called()
        self.assertEqual(asker.call_args.args[1]['argv'],['blender','--version'])


class PreviewArtifacts(unittest.TestCase):
    def test_mcp_preview_is_saved_without_claiming_visual_review(self):
        import base64
        from agentharness.mcp import McpConnection
        with tempfile.TemporaryDirectory() as tmp:
            connection = object.__new__(McpConnection)
            connection.asset_dir = Path(tmp)/'assets'
            payload = {'content':[{'type':'image','mimeType':'image/png',
                                   'data':base64.b64encode(b'preview fixture').decode()}]}
            with patch.object(connection,'_result',return_value=payload):
                text = connection.call('render',{})
            self.assertIn('review not performed',text)
            self.assertEqual(next(connection.asset_dir.iterdir()).read_bytes(),b'preview fixture')
