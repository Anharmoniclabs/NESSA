"""User-requested desktop opening after all textured character jobs complete."""
import fcntl
import json
from pathlib import Path
import subprocess
import time

root=Path.home()/'Projects/HeraldsOfTheCemi'
manifest=root/'production/character-3d-manifest.json'
lock=(root/'production/open-when-done.lock').open('a')
try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:raise SystemExit('A completion opener is already watching.')
marker=root/'production/open-when-done.json'
deadline=time.monotonic()+48*3600
while time.monotonic()<deadline:
    data=json.loads(manifest.read_text())
    rows=data.get('assets',[])
    if rows and all(r.get('finish_status')=='complete' for r in rows):
        first=root/rows[0]['blend']
        if not first.is_file():raise SystemExit('Completed manifest has no Blender artifact.')
        subprocess.run(['xdg-open',str(root/'production/character-3d-gallery.html')],check=True,timeout=30)
        subprocess.Popen([str(Path.home()/'.local/bin/blender'),str(first)],start_new_session=True)
        marker.write_text(json.dumps({'status':'opened','count':len(rows),'time':time.time(),'blend':str(first)},indent=2)+'\n')
        print('Opened all-model gallery and first textured Blender model.',flush=True)
        break
    pending=any(r['status'] not in ('complete','failed') for r in rows)
    if rows and not pending and any(r['status']=='failed' for r in rows):
        marker.write_text(json.dumps({'status':'blocked_by_failed_models','time':time.time()},indent=2)+'\n')
        subprocess.run(['notify-send','Nessa: 3D batch needs attention','Some models failed. The gallery shows the affected files.'],check=False)
        raise SystemExit('Not opening unfinished models as a complete batch; inspect manifest failures.')
    time.sleep(20)
else:
    raise SystemExit('Completion watcher reached 48-hour limit without a complete batch.')
