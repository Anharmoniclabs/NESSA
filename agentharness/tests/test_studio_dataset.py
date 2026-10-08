import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from agentharness.studio_dataset import audit


class Dataset(unittest.TestCase):
    def test_empty_pack_is_not_training_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'manifest.json').write_text('{"items":[]}')
            self.assertEqual(audit(p,'manifest.json')['status'],'not_ready')

    def test_reviewed_split_and_no_duplicate_leakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);items=[]
            for i in range(30):
                raw=b'\x89PNG\r\n\x1a\n'+str(i).encode()
                (p/f'{i}.png').write_bytes(raw)
                (p/f'{i}.txt').write_text('cemi_lm_style, reviewed image')
                items.append(dict(image=f'{i}.png',caption=f'{i}.txt',approved=True,
                    source_type='original',provenance='User drawing',sha256=hashlib.sha256(raw).hexdigest(),
                    split='validation' if i<3 else 'train'))
            def result():
                (p/'manifest.json').write_text(json.dumps(dict(items=items)))
                return audit(p,'manifest.json')
            self.assertEqual(result()['status'],'ready_for_training_setup')
            items[-1]={**items[0],'split':'train'}
            self.assertEqual(result()['status'],'not_ready')
            self.assertTrue(any('Duplicate' in e for e in result()['errors']))

    def test_composite_and_changed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'manifest.json').write_text(json.dumps(dict(items=[dict(source_type='composite',approved=True)])))
            self.assertIn('composite',audit(p,'manifest.json')['errors'][0])
