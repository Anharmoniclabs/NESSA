import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentharness import media_cloud as m
from agentharness import media_approval
from agentharness.cloud import HF_ROUTER
from agentharness.tools import build_tools
from agentharness.workspace import Workspace,ToolError


class MediaCloud(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.ctx=SimpleNamespace(ws=Workspace(self.root/'work',repo=self.root),
            client=SimpleNamespace(base_url=HF_ROUTER,api_key='hf_private'))
        (self.root/'ref.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.args=dict(prompt='subtle motion',image='ref.png',path='shot.mp4')
        self.response=json.dumps(dict(request_id='abc-123',
            response_url='https://queue.fal.run/fal-ai/wan/requests/abc-123')).encode()
        p=patch.object(m.shutil,'which',return_value='/usr/bin/tool');p.start();self.addCleanup(p.stop)
        p=patch.object(media_approval,'APPROVAL_ROOT',self.root/'outside-approval-store');p.start();self.addCleanup(p.stop)
        media_approval.approve_image(self.ctx.ws,'ref.png')

    def submit(self):
        with patch.object(m,'_fetch',return_value=self.response) as fetch:
            result=json.loads(m.submit(self.ctx,self.args,'video_generate'))
        return result,fetch

    def test_submission_receipt_has_no_secrets_or_inline_image(self):
        result,fetch=self.submit()
        self.assertEqual(result['status'],'queued')
        receipt=(self.root/'shot.cloud-job.json').read_text()
        self.assertNotIn('hf_private',receipt);self.assertNotIn('base64',receipt)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.kwargs['payload']['num_frames'],81)
        with patch.object(m,'_fetch') as fetch,self.assertRaises(ToolError):
            m.submit(self.ctx,self.args,'video_generate')
        fetch.assert_not_called()

    def test_resume_progress_does_not_post(self):
        self.submit()
        with patch.object(m,'_fetch',return_value=b'{"status":"IN_PROGRESS"}') as fetch:
            result=json.loads(m.collect(self.ctx,dict(job='shot.cloud-job.json')))
        self.assertEqual(result['status'],'running')
        self.assertNotIn('payload',fetch.call_args.kwargs)

    def test_complete_collect_and_recheck_hash(self):
        self.submit()
        result=json.dumps(dict(video=dict(url='https://v3.fal.media/files/a.mp4'),seed=42)).encode()
        with patch.object(m,'_fetch',side_effect=[b'{"status":"COMPLETED"}',result,b'movie']) as fetch, \
             patch.object(m,'media_info',return_value=dict(width=1280,height=720,frames='81')), \
             patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            out=json.loads(m.collect(self.ctx,dict(job='shot.cloud-job.json')))
        self.assertEqual(out['status'],'generated')
        self.assertNotIn('token',fetch.call_args_list[-1].kwargs)
        (self.root/'shot.mp4').write_bytes(b'changed')
        with self.assertRaises(ToolError):m.collect(self.ctx,dict(job='shot.cloud-job.json'))

    def test_lost_submission_is_not_replayed(self):
        with patch.object(m,'_fetch',side_effect=TimeoutError('hf_private')), \
             self.assertRaises(ToolError):
            m.submit(self.ctx,self.args,'video_generate')
        self.assertEqual(json.loads((self.root/'shot.cloud-job.json').read_text())['status'],'outcome_unknown')
        with patch.object(m,'_fetch') as fetch,self.assertRaises(ToolError):
            m.collect(self.ctx,dict(job='shot.cloud-job.json'))
        fetch.assert_not_called()

    def test_poll_error_keeps_request_for_resume(self):
        self.submit()
        with patch.object(m,'_fetch',side_effect=TimeoutError('hf_private')),self.assertRaises(ToolError) as error:
            m.collect(self.ctx,dict(job='shot.cloud-job.json'))
        self.assertNotIn('hf_private',str(error.exception))
        self.assertEqual(json.loads((self.root/'shot.cloud-job.json').read_text())['request_id'],'abc-123')

    def test_bad_paths_sizes_and_local_mode_do_not_call_cloud(self):
        for extra in ({'image':'../outside.png'},{'path':'../out.mp4'},{'frames':80},{'resolution':'4k'}):
            with patch.object(m,'_fetch') as fetch,self.assertRaises(ToolError):
                m.submit(self.ctx,{**self.args,**extra},'video_generate')
            fetch.assert_not_called()
        self.ctx.client.base_url='http://localhost:11435/v1'
        with patch.object(m,'_fetch') as fetch,self.assertRaises(ToolError):
            m.submit(self.ctx,self.args,'video_generate')
        fetch.assert_not_called()

    def test_image_edit_uses_references_without_logging_them(self):
        with patch.object(m,'_fetch',return_value=self.response) as fetch:
            m.submit(self.ctx,dict(prompt='new pose',images=['ref.png'],path='edit.png'),'image_edit')
        self.assertTrue(fetch.call_args.kwargs['payload']['image_urls'][0].startswith('data:image/png'))
        record=json.loads((self.root/'edit.cloud-job.json').read_text())
        self.assertEqual(record['model'],'Qwen/Qwen-Image-Edit-2509')
        self.assertEqual(len(record['references']),1)

    def test_tools_are_permission_gated(self):
        from agentharness.permissions import Permissions
        for name in ('image_edit','video_generate','media_job'):
            t=build_tools()[name]
            self.assertFalse(t.cacheable)
            self.assertFalse(Permissions('plan').check(name,t.kind,{})[0])

    def test_changed_image_requires_new_user_approval_before_network(self):
        (self.root/'ref.png').write_bytes(b'\x89PNG\r\n\x1a\nchanged')
        with patch.object(m,'_fetch') as fetch,self.assertRaisesRegex(ToolError,'User image approval required'):
            m.submit(self.ctx,self.args,'video_generate')
        fetch.assert_not_called()
        self.assertFalse((self.root/'shot.cloud-job.json').exists())

    def test_model_review_note_cannot_approve_image(self):
        media_approval.record_path(self.root,self.root/'ref.png').unlink()
        (self.root/'production').mkdir()
        (self.root/'production/state.json').write_text('{"reviews":[{"verdict":"accepted"}]}')
        with patch.object(m,'_fetch') as fetch,self.assertRaisesRegex(ToolError,'User image approval required'):
            m.submit(self.ctx,self.args,'video_generate')
        fetch.assert_not_called()
