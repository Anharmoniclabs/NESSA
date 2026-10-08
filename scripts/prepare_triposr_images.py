"""Copy authorized Downloads art, retaining original bytes and a provenance manifest."""
import hashlib
import json
from pathlib import Path
from PIL import Image

source=Path.home()/'Downloads'
root=Path.home()/'Projects/HeraldsOfTheCemi'
target=root/'sources/character-images'
target.mkdir(parents=True,exist_ok=True)
rows=[]
for p in sorted(source.glob('*.png')):
    if not (p.stem.isdigit() or p.stem in ('Untitled','Untitled2')):
        continue
    raw=p.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    dest=target/p.name
    if dest.exists() and dest.read_bytes()!=raw:
        raise RuntimeError('Source changed; preserve existing version: '+str(dest))
    dest.write_bytes(raw)
    im=Image.open(p).convert('RGBA')
    # These supplied PNGs are cutouts. Crop only transparent padding, then pad to
    # a square at 85% occupancy; preserve the artwork and all visible props.
    alpha=im.getchannel('A');box=alpha.point(lambda x:255 if x>8 else 0).getbbox()
    if box is None:raise RuntimeError('Empty image: '+p.name)
    im=im.crop(box)
    edge=int(max(im.size)/0.85)+2
    canvas=Image.new('RGBA',(edge,edge),(128,128,128,255))
    canvas.alpha_composite(im,((edge-im.width)//2,(edge-im.height)//2))
    prepared=target/(p.stem+'-prepared.png')
    canvas.convert('RGB').save(prepared)
    rows.append({'id':'source-'+p.stem.lower(),'source':str(dest.relative_to(root)),
        'original_download':str(p),'source_sha256':digest,
        'prepared':str(prepared.relative_to(root)),
        'prepared_sha256':hashlib.sha256(prepared.read_bytes()).hexdigest(),
        'character_name':None,'identity_status':'unassigned; do not infer identity from appearance',
        'canon_source':'User-supplied Character Bible, 2026-10-08',
        'status':'pending','output_dir':'models/triposr/'+p.stem.lower()})
manifest=root/'production/character-3d-manifest.json'
if manifest.exists():raise RuntimeError('Manifest exists; resume it instead of recreating.')
manifest.write_text(json.dumps({'version':1,'model':'stabilityai/TripoSR','assets':rows},indent=2)+'\n')
print(f'Prepared {len(rows)} illustrations: {manifest}')
