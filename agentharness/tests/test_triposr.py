import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentharness import triposr as m
from agentharness.tools import build_tools
from agentharness.workspace import Workspace, ToolError
from agentharness.permissions import Permissions


class TripoSR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = SimpleNamespace(ws=Workspace(self.root/'work', repo=self.root))
        (self.root/'input.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.info = dict(installed=True,root='/runtime',python='/runtime/python',model='stabilityai/TripoSR')

    def test_permission_and_schema(self):
        tools = build_tools()
        self.assertEqual(tools['mesh_generate'].kind, 'dev')
        self.assertEqual(tools['mesh_status'].kind, 'read')
        self.assertEqual(tools['mesh_finish'].kind, 'dev')
        self.assertFalse(Permissions('plan').check('mesh_finish','dev',{})[0])
        self.assertFalse(Permissions('plan').check('mesh_generate','dev',{})[0])
        tools['mesh_generate'].validate({'image':'input.png','output_dir':'models/a'})

    def test_bad_paths_and_resolution_never_launch(self):
        for extra in ({'image':'../outside.png'}, {'output_dir':'../outside'}, {'resolution':12}):
            with patch.object(m,'runtime',return_value=self.info), patch.object(m.subprocess,'Popen') as run:
                with self.assertRaises(ToolError):
                    m.generate(self.ctx,dict(image='input.png',output_dir='models/a',**{} ) | extra)
                run.assert_not_called()

    def test_missing_runtime_is_explicit(self):
        with patch.object(m,'runtime',return_value={'installed':False}), self.assertRaises(ToolError):
            m.generate(self.ctx,{'image':'input.png','output_dir':'models/a'})

    def test_submit_copies_source_and_does_not_repeat(self):
        with patch.object(m,'runtime',return_value=self.info), \
             patch('agentharness.media_cloud.media_info',return_value={'width':32,'height':32}), \
             patch.object(m.subprocess,'Popen') as run:
            args=dict(image='input.png',output_dir='models/a')
            result=json.loads(m.generate(self.ctx,args))
            self.assertEqual(result['status'],'submitted')
            self.assertEqual((self.root/'models/a/source.png').read_bytes(), (self.root/'input.png').read_bytes())
            with self.assertRaises(ToolError):m.generate(self.ctx,args)
            self.assertEqual(run.call_count,1)
            record=json.loads(m.status(self.ctx,{'job':result['job']}))
            self.assertEqual(record['status'],'starting')
            self.assertNotIn('argv',record)

    def test_invalid_mesh_is_not_success(self):
        path=self.root/'bad.glb';path.write_bytes(b'not a mesh')
        with self.assertRaises(ValueError):m.inspect_glb(path)

    def test_worker_failure_durable(self):
        job=self.root/'job.json'
        job.write_text(json.dumps(dict(argv=['fake'],cwd=str(self.root),timeout=1,started_at=0)))
        with patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            m.worker(job)
        self.assertEqual(json.loads(job.read_text())['status'],'failed')

    def test_worker_timeout_durable(self):
        job=self.root/'job.json'
        job.write_text(json.dumps(dict(argv=['fake'],cwd=str(self.root),timeout=1,started_at=0)))
        with patch.object(m.subprocess,'run',side_effect=m.subprocess.TimeoutExpired('fake',1)):
            m.worker(job)
        self.assertEqual(json.loads(job.read_text())['status'],'timeout')

    def test_finish_rejects_escape_and_existing_output(self):
        (self.root/'existing').mkdir()
        for extra in ({'mesh':'../outside.glb'}, {'output_dir':'existing'}):
            with patch.object(m,'runtime',return_value=self.info), \
                 patch.object(m.shutil,'which',return_value='/usr/bin/python3'), \
                 patch.object(m.subprocess,'Popen') as run:
                with self.assertRaises(ToolError):
                    m.finish(self.ctx,dict(mesh='a.glb',image='input.png',output_dir='new') | extra)
                run.assert_not_called()

    def test_finish_does_not_accept_missing_source_mesh(self):
        with patch.object(m,'runtime',return_value=self.info), \
             patch.object(m.shutil,'which',return_value='/usr/bin/python3'), \
             patch.object(m.subprocess,'Popen') as run, self.assertRaises(ToolError):
            m.finish(self.ctx,dict(mesh='missing.glb',image='input.png',output_dir='new'))
        run.assert_not_called()
