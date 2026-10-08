import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentharness import image_cloud as m
from agentharness.cloud import HF_ROUTER
from agentharness.permissions import Permissions
from agentharness.tools import build_tools
from agentharness.workspace import Workspace, ToolError


class ImageCloud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = SimpleNamespace(ws=Workspace(self.root/'work', repo=self.root),
            client=SimpleNamespace(base_url=HF_ROUTER, api_key='hf_test_secret'))
        self.args = dict(prompt='Night ocean anime', path='ocean.png')

    def test_local_session_cannot_spend_cloud_credits(self):
        self.ctx.client.base_url = 'http://localhost:11435/v1'
        with patch.object(m, '_fetch') as fetch, self.assertRaises(ToolError):
            m.generate(self.ctx, self.args)
        fetch.assert_not_called()

    def test_paths_and_dimensions_rejected_before_request(self):
        for extra in ({'path':'../outside.png'}, {'width':999}, {'seed':-1}, {'path':'x.jpg'}):
            with patch.object(m, '_fetch') as fetch, self.assertRaises(ToolError):
                m.generate(self.ctx, {**self.args, **extra})
            fetch.assert_not_called()

    def test_receipt_and_no_token_on_download_or_disk(self):
        png = b'\x89PNG\r\n\x1a\n' + b'\0\0\0\rIHDR' + struct.pack('>II',1664,928)
        result = json.dumps({'images':[{'url':'https://v3.fal.media/files/test.png'}], 'seed':42}).encode()
        with patch.object(m, '_fetch', side_effect=[result,png]) as fetch, \
             patch.object(m.shutil,'which',return_value='/usr/bin/ffmpeg'), \
             patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            out = json.loads(m.generate(self.ctx,self.args))
        self.assertEqual(out['status'],'generated')
        self.assertNotIn('token',fetch.call_args_list[1].kwargs)
        self.assertNotIn('hf_test_secret',(self.root/'ocean.generation.json').read_text())
        self.assertEqual((self.root/'ocean.png').read_bytes(),png)
        with patch.object(m,'_fetch') as fetch, self.assertRaises(ToolError):
            m.generate(self.ctx,self.args)
        fetch.assert_not_called()

    def test_failure_is_durable_and_exception_secrets_are_hidden(self):
        with patch.object(m.shutil,'which',return_value='/usr/bin/ffmpeg'), \
             patch.object(m,'_fetch',side_effect=RuntimeError('hf_test_secret')):
            with self.assertRaises(ToolError) as e:
                m.generate(self.ctx,self.args)
        self.assertNotIn('hf_test_secret',str(e.exception))
        receipt=json.loads((self.root/'ocean.generation.json').read_text())
        self.assertEqual(receipt['status'],'failed')
        self.assertFalse((self.root/'ocean.png').exists())

    def test_untrusted_download_is_rejected(self):
        result=json.dumps({'images':[{'url':'https://localhost/private'}]}).encode()
        with patch.object(m.shutil,'which',return_value='/usr/bin/ffmpeg'), \
             patch.object(m,'_fetch',return_value=result) as fetch, self.assertRaises(ToolError):
            m.generate(self.ctx,self.args)
        self.assertEqual(fetch.call_count,1)

    def test_permission_gate_and_schema(self):
        tool=build_tools()['image_generate']
        self.assertEqual(tool.kind,'dev')
        self.assertFalse(tool.cacheable)
        self.assertFalse(Permissions('plan').check(tool.name,tool.kind,self.args)[0])
        self.assertFalse(Permissions('accept_edits').check(tool.name,tool.kind,self.args)[0])
        self.assertEqual(tool.validate(self.args),self.args)

    def test_model_routes_steps_and_schema(self):
        self.assertEqual(m.DEFAULT_MODEL, 'black-forest-labs/FLUX.1-dev')
        cases = [('black-forest-labs/FLUX.1-dev', 'fal-ai/flux/dev', 28),
                 ('black-forest-labs/FLUX.1-schnell', 'fal-ai/flux/schnell', 4),
                 ('Qwen/Qwen-Image-2512', 'fal-ai/qwen-image-2512', 50)]
        png = b'\x89PNG\r\n\x1a\n' + b'\0\0\0\rIHDR' + struct.pack('>II',1664,928)
        result = json.dumps({'images':[{'url':'https://v3.fal.media/test.png'}]}).encode()
        for index, (model, route, steps) in enumerate(cases):
            with self.subTest(model=model):
                args = {**self.args, 'model':model, 'path':f'model-{index}.png'}
                self.assertEqual(build_tools()['image_generate'].validate(args), args)
                with patch.object(m, '_fetch', side_effect=[result,png]) as fetch, \
                     patch.object(m.shutil,'which',return_value='/usr/bin/ffmpeg'), \
                     patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                    out = json.loads(m.generate(self.ctx,args))
                call = fetch.call_args_list[0]
                self.assertEqual(call.args[0], 'https://router.huggingface.co/fal-ai/' + route)
                self.assertEqual(call.kwargs['payload']['num_inference_steps'], steps)
                self.assertEqual(out['model'], model)

    def test_unknown_model_never_substituted(self):
        with patch.object(m, '_fetch') as fetch, self.assertRaises(ToolError):
            m.generate(self.ctx, {**self.args, 'model':'unknown-flux'})
        fetch.assert_not_called()
