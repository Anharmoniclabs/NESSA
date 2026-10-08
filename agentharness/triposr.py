"""Local single-image reconstruction with durable, explicitly polled jobs."""
import hashlib
import json
import os
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import time

from .session import atomic_json
from .workspace import ToolError


def runtime():
    root = Path(os.environ.get('NESSA_TRIPOSR_HOME', str(Path.home()/'.local/opt/TripoSR'))).resolve()
    python = root/'.venv/bin/python'
    ready = root/'nessa-runtime.json'
    return dict(root=str(root), python=str(python), installed=bool(
        python.is_file() and (root/'run.py').is_file() and ready.is_file()),
        backend='local', model='stabilityai/TripoSR',
        note='Single-object reconstruction; output is an unrigged mesh. CPU can be slow.')


def status(ctx, args):
    if not args.get('job'):
        return json.dumps(runtime())
    path = ctx.ws.path(args['job'])
    if path.name != 'job.json' or not path.is_file() or path.stat().st_size > 100_000:
        raise ToolError('Use the job.json returned by mesh_generate.')
    record = json.loads(path.read_text())
    if record.get('kind') not in ('triposr','mesh-finish'):
        raise ToolError('Not a TripoSR job.')
    return json.dumps({k:record.get(k) for k in (
        'status','image','output','blend','sha256','input_sha256','mesh','error','seconds','pid')})[:4000]


def inspect_glb(path):
    size = path.stat().st_size
    if not 32 <= size <= 200_000_000:
        raise ValueError('Invalid mesh size')
    with path.open('rb') as f:
        magic, version, length = struct.unpack('<4sII', f.read(12))
        if magic != b'glTF' or version != 2 or length != size:
            raise ValueError('Invalid GLB header')
        count, kind = struct.unpack('<I4s', f.read(8))
        if kind != b'JSON' or count > min(size-20, 10_000_000):
            raise ValueError('Invalid GLB JSON chunk')
        doc = json.loads(f.read(count))
    primitives = [p for mesh in doc.get('meshes', []) for p in mesh.get('primitives', [])]
    vertices = sum(doc['accessors'][p['attributes']['POSITION']]['count'] for p in primitives)
    if not primitives or vertices < 3:
        raise ValueError('No mesh geometry')
    return dict(primitives=len(primitives), vertices=vertices, bytes=size,
                validation='GLB structure and geometry counts; visual review required')


def generate(ctx, args):
    info = runtime()
    if not info['installed']:
        raise ToolError('TripoSR runtime is not ready. See docs/TRIPOSR.md; mesh_status reports installation.')
    image = ctx.ws.path(args['image'])
    output = ctx.ws.path(args['output_dir'])
    resolution = args.get('resolution', 128)
    if type(resolution) is not int or resolution not in (64, 128, 256):
        raise ToolError('resolution must be 64, 128 or 256.')
    if not image.is_file() or image.stat().st_size > 20_000_000:
        raise ToolError('Input must be an existing project image, at most 20 MB.')
    data = image.read_bytes()
    if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')):
        raise ToolError('Use a PNG or JPEG image.')
    if output.exists():
        raise ToolError('Output directory already exists. Poll its job.json; never overwrite or resubmit.')
    from .media_cloud import media_info
    metadata = media_info(image)
    if not metadata.get('width') or not metadata.get('height') or metadata['width']*metadata['height'] > 40_000_000:
        raise ToolError('Image dimensions are invalid or exceed 40 megapixels.')
    output.mkdir(parents=True)
    source = output/('source.png' if data.startswith(b'\x89PNG') else 'source.jpg')
    source.write_bytes(data)
    # Upstream --no-remove-bg does not create this directory itself.
    (output/'0').mkdir()
    argv = [info['python'], str(Path(info['root'])/'run.py'), str(source),
            '--device','cpu','--chunk-size','2048','--mc-resolution',str(resolution),
            '--output-dir',str(output),'--model-save-format','glb']
    if not args.get('remove_background', True):
        argv.append('--no-remove-bg')
    job = output/'job.json'
    record = dict(kind='triposr', status='starting', model=info['model'], backend='local',
                  image=ctx.ws.rel(image), input_sha256=hashlib.sha256(data).hexdigest(),
                  output=ctx.ws.rel(output/'0/mesh.glb'), argv=argv,
                  cwd=info['root'], started_at=time.time(), timeout=1800)
    atomic_json(job, record)
    try:
        with (output/'worker.log').open('ab') as log:
            subprocess.Popen([sys.executable,'-m','agentharness.triposr',str(job)],
                cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
    except Exception:
        record.update(status='failed', error='Could not start reconstruction worker')
        atomic_json(job, record)
        raise ToolError(record['error']) from None
    return json.dumps(dict(status='submitted', job=ctx.ws.rel(job), output=record['output'],
                           next_action='Poll mesh_status with this job; do not resubmit.'))


def finish(ctx, args):
    info=runtime()
    blender=shutil.which('blender') or str(Path.home()/'.local/bin/blender')
    if not info['installed'] or not Path(blender).is_file():
        raise ToolError('The TripoSR runtime and Blender must be installed before finishing.')
    mesh=ctx.ws.path(args['mesh']);image=ctx.ws.path(args['image']);output=ctx.ws.path(args['output_dir'])
    if output.exists():
        raise ToolError('Finishing directory exists. Poll job.json or use a new version; never overwrite.')
    if not image.is_file() or image.stat().st_size>20_000_000:
        raise ToolError('Source artwork must exist and be at most 20 MB.')
    if not mesh.is_file():raise ToolError('Source mesh not found.')
    inspect_glb(mesh)
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    name=output.name
    output.mkdir(parents=True)
    record=dict(kind='mesh-finish',status='starting',model='TripoSR + source artwork projection',
                backend='local-blender',image=ctx.ws.rel(image),
                input_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                source_mesh_sha256=hashlib.sha256(mesh.read_bytes()).hexdigest(),
                output=ctx.ws.rel(output/(name+'.glb')),blend=ctx.ws.rel(output/(name+'.blend')),
                commands=[[info['python'],str(scripts/'prepare_character_texture.py'),str(image),str(output)],
                          [blender,'--background','--factory-startup','--threads','2','--python-exit-code','1',
                           '--python',str(scripts/'finish_character.py'),'--',str(mesh),str(image),str(output)]],
                cwd=str(ctx.ws.repo),started_at=time.time(),timeout=900)
    job=output/'job.json';atomic_json(job,record)
    try:
        with (output/'worker.log').open('ab') as log:
            subprocess.Popen([sys.executable,'-m','agentharness.triposr',str(job)],
                cwd=str(Path(__file__).resolve().parents[1]),stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,start_new_session=True)
    except Exception:
        record.update(status='failed',error='Could not start finishing worker');atomic_json(job,record)
        raise ToolError(record['error']) from None
    return json.dumps(dict(status='submitted',job=ctx.ws.rel(job),blend=record['blend'],
                           next_action='Poll mesh_status; textured static model, visual review required.'))


def worker(job):
    record = json.loads(job.read_text())
    record.update(status='running', pid=os.getpid())
    atomic_json(job, record)
    env = dict(os.environ, OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
    # Download public model weights when necessary, without forwarding account credentials.
    env.pop('HF_TOKEN', None)
    env['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    try:
        with (job.parent/'inference.log').open('wb') as log:
            commands=record.get('commands') or [record['argv']]
            deadline=time.monotonic()+record['timeout']
            for command in commands:
                run = subprocess.run(command, cwd=record['cwd'], env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=max(1,deadline-time.monotonic()))
                if run.returncode:
                    raise RuntimeError('Mesh operation exited unsuccessfully; inspect inference.log')
        if record.get('kind')=='mesh-finish':
            mesh=job.parent/(job.parent.name+'.glb')
            report=json.loads((job.parent/'validation.json').read_text())
            if report.get('status')!='technically_valid' or not report.get('packed'):
                raise RuntimeError('Blender package readback failed')
            for filename in ('front.png','three-quarter.png','back.png','albedo.png',job.parent.name+'.blend'):
                if not (job.parent/filename).is_file():raise RuntimeError('Missing finishing artifact: '+filename)
        else:
            mesh = job.parent/'0/mesh.glb'
        record['mesh'] = inspect_glb(mesh)
        record['sha256'] = hashlib.sha256(mesh.read_bytes()).hexdigest()
        record['status'] = 'generated'
    except subprocess.TimeoutExpired:
        record.update(status='timeout', error='Mesh operation exceeded its time budget; partial files are not success.')
    except Exception as exc:
        record.update(status='failed', error=str(exc)[:500])
    record['seconds'] = time.time()-record['started_at']
    atomic_json(job, record)


if __name__ == '__main__':
    worker(Path(sys.argv[1]).resolve())
