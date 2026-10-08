"""Resume the authorized character-art batch using Nessa tools, one CPU job at a time."""
import fcntl
import json
from pathlib import Path
import shutil
import sys
import time
import html

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentharness.agent import Agent, AgentConfig
from agentharness.llm import ChatClient, ToolCall
from agentharness.session import atomic_json
from agentharness.workspace import Workspace

root=Path.home()/'Projects/HeraldsOfTheCemi'
manifest=root/'production/character-3d-manifest.json'
lock=manifest.with_suffix('.lock').open('a')
try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:raise SystemExit('A character conversion batch is already running.')
data=json.loads(manifest.read_text())
ws=Workspace.direct(root,Path.home()/'.agentharness/runs'/('cemi-3d-'+str(time.time_ns())))
agent=Agent(ChatClient('http://127.0.0.1:11435/v1','nessa-lfm-32k'),ws,
            config=AgentConfig(require_approval=False,permission_mode='bypass'))
agent.task='User authorized all supplied character illustrations to TripoSR meshes and Blender files. Deterministic batch; no animation.'
print('Evidence:',agent.evidence_dir,flush=True)
blender=shutil.which('blender') or str(Path.home()/'.local/bin/blender')
recipe=root/'scripts/mesh_to_blend.py'
shutil.copy2(Path(__file__).with_name('mesh_to_blend.py'),recipe)

def save_progress():
    atomic_json(manifest,data)
    cards=[]
    for item in data['assets']:
        label=html.escape(item['id']);state=html.escape(item['status'])
        source='../'+item['source']
        links=''
        if item.get('blend') and (root/item['blend']).is_file():
            links=f'<a href="../{html.escape(item["blend"])}">Blender file</a> · <a href="../{html.escape(item["mesh"])}">GLB mesh</a>'
        preview=item.get('preview') or str(Path(item.get('blend','missing')).with_suffix('.png'))
        thumb='../'+preview if (root/preview).is_file() else source
        cards.append(f'<article><img src="{html.escape(thumb)}"><h2>{label}</h2><p>{state}</p>{links}<p><a href="{html.escape(source)}">Original art</a></p></article>')
    done=sum(r.get('finish_status')=='complete' for r in data['assets'])
    page='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>Cemí 3D models</title><style>body{font:16px system-ui;background:#171a20;color:#eee;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}article{background:#252b35;padding:16px;border-radius:12px}img{width:100%;height:240px;object-fit:contain}a{color:#9cd7ff}h2{font-size:18px}</style>'
    page+=f'<h1>Cemí 3D models — {done}/{len(data["assets"])} complete</h1><p>Textured display models · 2K artwork · Unrigged · Visual review pending · Names retain source filenames</p><main>'+''.join(cards)+'</main>'
    (root/'production/character-3d-gallery.html').write_text(page)

save_progress()

def call(name,**args):
    out=agent._execute(ToolCall(name+str(time.time_ns()),name,args),[name],dedupe=False)
    if out.startswith(('ERROR:','TIMEOUT')):raise RuntimeError(out[:800])
    return out

for row in data['assets']:
    if row.get('finish_status')=='complete':continue
    if row['status']=='failed':continue
    print('START',row['id'],flush=True)
    try:
        job=root/row['output_dir']/'job.json'
        if not job.exists():
            call('mesh_generate',image=row['prepared'],output_dir=row['output_dir'],resolution=256,remove_background=False)
        row['status']='reconstructing';save_progress()
        deadline=time.monotonic()+1900
        while True:
            result=json.loads(call('mesh_status',job=str(job.relative_to(root))))
            if result['status'] not in ('starting','running'):break
            if time.monotonic()>deadline:raise RuntimeError('Job wait exceeded budget; inspect worker before resuming.')
            time.sleep(10)
        if result['status']!='generated':raise RuntimeError(result.get('error') or result['status'])
        mesh=result['output']
        row['mesh']=mesh
        call('production_update',operation='asset',data=dict(id=row['id']+'-mesh',path=mesh,role='unrigged-mesh',notes='TripoSR; source '+row['source']))
        finish_dir='models/finished-textured/'+row['id']
        finish_job=root/finish_dir/'job.json'
        if not finish_job.exists():
            call('mesh_finish',mesh=mesh,image=row['source'],output_dir=finish_dir)
        row['status']='texturing';save_progress()
        deadline=time.monotonic()+1000
        while True:
            finished=json.loads(call('mesh_status',job=str(finish_job.relative_to(root))))
            if finished['status'] not in ('starting','running'):break
            if time.monotonic()>deadline:raise RuntimeError('Finishing wait exceeded budget; inspect worker before resuming.')
            time.sleep(10)
        if finished['status']!='generated':raise RuntimeError(finished.get('error') or finished['status'])
        row['blend']=finished['blend'];row['textured_mesh']=finished['output']
        row['preview']=finish_dir+'/front.png'
        validation=json.loads((root/finish_dir/'validation.json').read_text())
        if validation.get('status')!='technically_valid':raise RuntimeError('Textured Blender readback failed')
        call('production_update',operation='asset',data=dict(id=row['id']+'-textured-blend',path=row['blend'],role='blender-source',notes='2K original-art texture packed. Unrigged display mesh. Geometric and hidden-surface visual review pending.'))
        call('production_update',operation='asset',data=dict(id=row['id']+'-textured-glb',path=row['textured_mesh'],role='textured-mesh',notes='Same textured display mesh exported to GLB.'))
        row['status']='complete';row['finish_status']='complete';row['validation']=validation
        print('DONE TEXTURED',row['id'],row['blend'],flush=True)
    except Exception as exc:
        row['status']='failed';row['error']=str(exc)[:800]
        print('FAILED',row['id'],row['error'],flush=True)
    save_progress()
summary={s:sum(r['status']==s for r in data['assets']) for s in ('complete','failed','pending')}
print('SUMMARY',json.dumps(summary),flush=True)
raise SystemExit(1 if summary['failed'] or summary['pending'] else 0)
