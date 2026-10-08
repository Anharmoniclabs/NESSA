"""Dataset readiness checks; metadata approval is not automated aesthetic review."""
import hashlib
import json
from pathlib import Path
from .workspace import ToolError


def audit(root,manifest='dataset/manifest.json'):
    root=Path(root).resolve()
    def local(name):
        if not isinstance(name,str) or not name:raise ToolError('Missing dataset path.')
        p=(root/name).resolve()
        if not p.is_relative_to(root):raise ToolError('Dataset path escapes the project.')
        return p
    file=local(manifest)
    if file.stat().st_size>1_000_000:raise ToolError('Dataset manifest exceeds 1 MB.')
    data=json.loads(file.read_text())
    items=data.get('items',[])
    if not isinstance(items,list) or len(items)>500:raise ToolError('Use a list of at most 500 dataset entries.')
    errors=[];counts=dict(train=0,validation=0);seen={};approved=[]
    trigger=data.get('trigger','cemi_lm_style')
    for n,row in enumerate(items):
        label=f'entry {n+1}'
        try:
            if not isinstance(row,dict):raise ToolError('Entry must be an object.')
            if row.get('approved') is not True:raise ToolError('Requires explicit image approval.')
            if row.get('source_type') not in ('original','licensed','generated'):
                raise ToolError('Source type must be original, licensed or generated; composite moodboards are excluded.')
            if not row.get('provenance'):raise ToolError('Missing provenance note.')
            split=row.get('split')
            if split not in counts:raise ToolError('Split must be train or validation.')
            image=local(row.get('image'));caption=local(row.get('caption'))
            if not image.is_file() or image.stat().st_size>20_000_000:raise ToolError('Image missing or exceeds 20 MB.')
            raw=image.read_bytes()
            if not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')):
                raise ToolError('Image must be PNG/JPEG.')
            digest=hashlib.sha256(raw).hexdigest()
            if row.get('sha256')!=digest:raise ToolError('Image hash does not match reviewed version.')
            if digest in seen:raise ToolError('Duplicate image, including possible train/validation leakage.')
            if not caption.is_file() or caption.stat().st_size>8000:raise ToolError('Caption missing or too large.')
            text=caption.read_text().strip()
            if not text or trigger not in text:raise ToolError('Caption must include the style trigger.')
            seen[digest]=split;counts[split]+=1;approved.append(row)
        except (ToolError,OSError,ValueError,TypeError) as exc:
            errors.append(label+': '+str(exc)[:200])
    if sum(counts.values())<30:errors.append('Need at least 30 distinct approved images for this configured pilot gate.')
    if counts['validation']<3:errors.append('Keep at least 3 approved images in the held-out validation split.')
    if counts['train']<27:errors.append('Need at least 27 training images; validation images stay separate.')
    return dict(status='ready_for_training_setup' if not errors else 'not_ready',counts=counts,
        errors=errors[:30],error_count=len(errors),trigger=trigger,
        note='Count/hash/caption/provenance checks only; diversity and aesthetic quality require review.')


def check(ctx,args):
    return json.dumps(audit(ctx.ws.repo,args.get('manifest','dataset/manifest.json')))[:10000]
