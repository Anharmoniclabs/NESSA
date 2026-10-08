"""Resumable Qwen reference editing and Wan I2V through the HF fal queue.

Submitting and collecting are separate bounded tool calls. Receipts survive process
exit; lost submit responses are never replayed automatically. No background agent.
"""
import base64
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
from urllib.parse import urlparse

from .cloud import HF_ROUTER
from .image_cloud import _fetch
from .session import atomic_json
from .workspace import ToolError
from .media_approval import require_approved

ROUTES = {'image_edit': ('Qwen/Qwen-Image-Edit-2509', 'fal-ai/qwen-image-edit-2509', '.png'),
          'video_generate': ('Wan-AI/Wan2.2-I2V-A14B', 'fal-ai/wan/v2.2-a14b/image-to-video', '.mp4')}
BASE = 'https://router.huggingface.co/fal-ai'
SUFFIX = '?_subdomain=queue'


def credential(ctx):
    c = getattr(ctx, 'client', None)
    for c in getattr(c, 'clients', [c]):
        if getattr(c, 'base_url', '').rstrip('/') == HF_ROUTER:
            key = getattr(c, 'api_key', '')
            if key.startswith('hf_'):
                return key
    raise ToolError('Requires an explicitly configured Hugging Face cloud session (--cloud).')


def media_info(path):
    result = subprocess.run(['ffprobe','-v','error','-count_frames','-show_streams',
                             '-show_format','-of','json',str(path)],
                            capture_output=True,text=True,timeout=40)
    if result.returncode:
        raise ToolError('Media metadata could not be decoded.')
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    return dict(width=v.get('width'), height=v.get('height'), codec=v.get('codec_name'),
                fps=v.get('avg_frame_rate'), frames=v.get('nb_read_frames'),
                duration=data.get('format',{}).get('duration'))


def summary(record, receipt):
    return json.dumps(dict(job=str(receipt),status=record['status'],model=record['model'],
        output=record['output'],request_id=record.get('request_id'),
        media=record.get('media'),sha256=record.get('sha256'),
        quality='visual/motion review required',next_action='media_job to check/collect; never resubmit this job'))[:4000]


def submit(ctx, args, kind):
    key = credential(ctx)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise ToolError('FFmpeg and ffprobe are required to verify outputs.')
    model, route, suffix = ROUTES[kind]
    prompt = args['prompt'].strip()
    if not 1 <= len(prompt) <= 6000:
        raise ToolError('Prompt must contain 1–6000 characters.')
    seed = args.get('seed',42)
    if type(seed) is not int or not 0 <= seed < 2**31:
        raise ToolError('Seed must be within 0..2147483647.')
    dest = ctx.ws.path(args['path'])
    if dest.suffix != suffix:
        raise ToolError(f'Output must have {suffix} extension.')
    receipt = dest.with_suffix('.cloud-job.json')
    if dest.exists() or receipt.exists():
        raise ToolError('Output/job already exists. Resume with media_job; do not resubmit.')
    images = args['images'] if kind == 'image_edit' else [args['image']]
    if not isinstance(images,list) or not 1 <= len(images) <= 3:
        raise ToolError('Use 1–3 reference images.')
    refs, uris = [], []
    for name in images:
        p = ctx.ws.path(name)
        if not p.is_file() or p.stat().st_size > 8_000_000:
            raise ToolError('Each reference must exist and be at most 8 MB.')
        data = p.read_bytes()
        if kind == 'video_generate':
            require_approved(ctx.ws,name,data)
        mime = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else None
        if not mime:
            raise ToolError('Reference images must be PNG or JPEG.')
        refs.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest()))
        uris.append('data:'+mime+';base64,'+base64.b64encode(data).decode())
    payload = dict(prompt=prompt, seed=seed)
    if kind == 'image_edit':
        payload.update(image_urls=uris,image_size='landscape_16_9',num_inference_steps=50,
                       num_images=1,output_format='png')
    else:
        frames = args.get('frames',81)
        resolution = args.get('resolution','720p')
        if type(frames) is not int or not 17 <= frames <= 161 or (frames-1)%4:
            raise ToolError('Video frames must be 4n+1, between 17 and 161.')
        if resolution not in ('480p','720p'):
            raise ToolError('Resolution must be 480p or 720p.')
        payload.update(image_url=uris[0],num_frames=frames,frames_per_second=16,
            resolution=resolution,aspect_ratio='16:9',num_inference_steps=27,
            interpolator_model='none',num_interpolated_frames=0,
            enable_prompt_expansion=False)
    dest.parent.mkdir(parents=True,exist_ok=True)
    record = dict(version=1,kind=kind,model=model,provider='fal-ai',transport='huggingface-router',
        output=ctx.ws.rel(dest),status='submitting',created_at=time.time(),references=refs,
        parameters={k:v for k,v in payload.items() if k not in ('image_url','image_urls')},
        billed_cost=None)
    with receipt.open('x') as f:
        json.dump(record,f,indent=2)
        f.flush()
    try:
        response = json.loads(_fetch(BASE+'/'+route+SUFFIX,token=key,payload=payload,timeout=60))
        parsed = urlparse(response['response_url'])
        if parsed.hostname != 'queue.fal.run' or parsed.scheme != 'https':
            raise ToolError('Unexpected queue result host.')
        result_path = parsed.path
        if not re.fullmatch(r'/fal-ai/[A-Za-z0-9/_-]+/requests/[A-Za-z0-9-]+',result_path):
            raise ToolError('Unexpected queue result path.')
        record.update(status='queued',request_id=response['request_id'],result_path=result_path)
    except BaseException as exc:
        record.update(status='outcome_unknown',error_type=type(exc).__name__)
        if isinstance(exc,urllib.error.HTTPError):record['http_status']=exc.code
        if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        raise ToolError(f'Cloud submission outcome unknown ({type(exc).__name__}); inspect {ctx.ws.rel(receipt)}. Do not retry automatically.') from None
    finally:
        atomic_json(receipt,record)
    return summary(record,ctx.ws.rel(receipt))


def collect(ctx,args):
    key = credential(ctx)
    receipt = ctx.ws.path(args['job'])
    if not receipt.name.endswith('.cloud-job.json') or not receipt.is_file() or receipt.stat().st_size>50000:
        raise ToolError('Provide an existing project .cloud-job.json receipt.')
    with receipt.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record = json.loads(receipt.read_text())
        dest = ctx.ws.path(record['output'])
        if record['status'] == 'generated':
            if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest()!=record['sha256']:
                raise ToolError('Previously generated artifact is missing or changed.')
            return summary(record,ctx.ws.rel(receipt))
        if record['status'] in ('submitting','outcome_unknown','failed'):
            raise ToolError('Job cannot be automatically replayed; inspect its receipt.')
        path = record.get('result_path','')
        if not re.fullmatch(r'/fal-ai/[A-Za-z0-9/_-]+/requests/[A-Za-z0-9-]+',path):
            raise ToolError('Invalid queue path in receipt.')
        try:
            status = json.loads(_fetch(BASE+path+'/status'+SUFFIX,token=key,timeout=30))
            state = status.get('status')
            record.update(provider_status=state,checked_at=time.time())
            if state in ('IN_QUEUE','IN_PROGRESS'):
                record['status']='queued' if state=='IN_QUEUE' else 'running'
                atomic_json(receipt,record)
                return summary(record,ctx.ws.rel(receipt))
            if state != 'COMPLETED':
                record['status']='failed'
                atomic_json(receipt,record)
                raise ToolError('Provider reported an unexpected terminal job state.')
            result = json.loads(_fetch(BASE+path+SUFFIX,token=key,timeout=30))
            if result.get('error'):
                record['status']='failed'
                atomic_json(receipt,record)
                raise ToolError('Provider completed with an error; no output collected.')
            url = result['images'][0]['url'] if record['kind']=='image_edit' else result['video']['url']
            u = urlparse(url)
            if (u.scheme!='https' or u.username or u.password or u.port not in (None,443) or
                not (u.hostname=='fal.media' or (u.hostname or '').endswith('.fal.media'))):
                raise ToolError('Unexpected media CDN host.')
            data = _fetch(url,limit=100_000_000,timeout=60)  # no HF token on CDN
            temporary = dest.with_name(dest.stem+'.download'+dest.suffix)
            if dest.exists():
                raise ToolError('Output exists without a completed receipt; inspect before collecting.')
            try:
                with temporary.open('xb') as f:f.write(data)
                info = media_info(temporary)
                decoded = subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(temporary),'-f','null','-'],
                    capture_output=True,timeout=60)
                if decoded.returncode or not info['width'] or not info['height']:
                    raise ToolError('Generated media failed decoding checks.')
                if record['kind']=='video_generate':
                    if int(info['frames'] or 0)!=record['parameters']['num_frames']:
                        raise ToolError('Generated frame count differs from requested count.')
                elif info['codec']!='png':
                    raise ToolError('Generated image is not PNG.')
                # Exclusive create prevents concurrent overwrite of existing art.
                with dest.open('xb') as f:f.write(data)
            finally:
                temporary.unlink(missing_ok=True)
            record.update(status='generated',media=info,sha256=hashlib.sha256(data).hexdigest(),
                completed_at=time.time(),returned_seed=result.get('seed'),bytes=len(data))
            atomic_json(receipt,record)
            return summary(record,ctx.ws.rel(receipt))
        except ToolError:
            raise
        except Exception as exc:
            # Keep queued ID for later collection; never repeat the paid submission.
            record['last_poll_error']=type(exc).__name__
            if isinstance(exc,urllib.error.HTTPError):record['last_http_status']=exc.code
            atomic_json(receipt,record)
            raise ToolError(f'Cloud collection failed ({type(exc).__name__}); resume the same job later.') from None
