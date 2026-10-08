"""Human image approval, separate from model-authored production review notes.

Only the terminal's explicit /approve-image command writes approvals. There is
deliberately no agent tool that can approve an image on the user's behalf.
"""
import hashlib
import json
from pathlib import Path
import time
from .session import atomic_json
from .workspace import ToolError

APPROVAL_ROOT = Path.home()/'.agentharness/media-approvals'


def record_path(root,path):
    key=hashlib.sha256((str(Path(root).resolve())+'\n'+str(Path(path).resolve())).encode()).hexdigest()
    return APPROVAL_ROOT/(key+'.json')


def approve_image(ws,name):
    """Called by the user-facing command handler, never registered as a tool."""
    path=ws.path(name)
    if not path.is_file() or path.suffix.lower() not in ('.png','.jpg','.jpeg'):
        raise ToolError('Approve an existing project PNG/JPEG image.')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    record=dict(path=ws.rel(path),sha256=digest,approved_at=time.time(),
                source='explicit_user_terminal_command',scope='animate_this_exact_image')
    atomic_json(record_path(ws.repo,path),record)
    return record


def require_approved(ws,name,data):
    path=ws.path(name)
    try:
        record=json.loads(record_path(ws.repo,path).read_text())
    except (OSError,ValueError):
        record={}
    digest=hashlib.sha256(data).hexdigest()
    if (record.get('sha256')!=digest or record.get('source')!='explicit_user_terminal_command'
            or record.get('scope')!='animate_this_exact_image'):
        raise ToolError('User image approval required before animation. Present '+ws.rel(path)+
            ' and wait for the user. In Nessa terminal: /approve-image '+ws.rel(path)+
            '. Agent review notes and permission bypass do not count as image approval.')
    return record
