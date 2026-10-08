"""Lazy adapters to installed creative engines; mutations use harness permissions."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from .checks import run_command
from .config import McpServer
from .mcp import shared_bus, tool_name
from .workspace import ToolError

APPS = {
    'photocraft': ('photocraft-cli', 'Raster painting, retouching, layers and image exports'),
    'vectorcraft': ('vectorcraft-cli', 'Vector paths, typography, illustration and SVG/PDF'),
    'filmcraft': ('filmcraft-cli', 'Timeline editing, audio, titles and video delivery'),
    'effectcraft': ('effectcraft-cli', 'Motion graphics, compositing, keyframes and effects'),
    'lightcraft': ('lightcraft-cli', 'Photo development, library, grading and image export'),
    'pdfcraft': ('printcraft-cli', 'PDF inspection, assembly, annotations and delivery'),
    'designcraft': ('designcraft-cli', 'Page layout, typography and publication'),
}


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    local = Path.home()/'.local/bin'/name
    return str(local) if local.is_file() else None


def inventory():
    from .triposr import runtime
    return json.dumps({**{name:dict(executable=executable(binary), purpose=purpose,
                                  mode='headless MCP; GUI connection is separate')
                         for name,(binary,purpose) in APPS.items()},
        'triposr':runtime(),
        'blender':dict(executable=executable('blender'),purpose='3D scenes, rigging, animation and rendering'),
        'ffmpeg':dict(executable=executable('ffmpeg'),purpose='Media assembly and encoding'),
        'ffprobe':dict(executable=executable('ffprobe'),purpose='Independent media inspection')}, indent=2)


def bus(ctx, app):
    if app not in APPS:
        raise ToolError('Unknown creative app; use creative_apps.')
    binary = executable(APPS[app][0])
    if not binary:
        raise ToolError(f'{app} automation executable is not installed.')
    argv = [binary, 'mcp']
    if app == 'vectorcraft':
        argv += ['--headless']
    elif app == 'photocraft':
        argv += ['--automation-read-root',str(ctx.ws.repo),'--automation-write-root',str(ctx.ws.repo)]
    elif app == 'pdfcraft':
        argv += ['--root',str(ctx.ws.repo)]
    elif app == 'lightcraft':
        argv += ['--compact']
    server = McpServer(app, 'stdio', argv=argv, timeout=60)
    result = shared_bus({app:server},ctx.ws.repo,ctx.evidence_dir/'creative')
    if result.errors:
        raise ToolError(json.dumps(result.errors))
    return result


def discover(ctx, args):
    b = bus(ctx,args['app'])
    query = args.get('query','').lower()
    rows = [spec for _,spec in b.discovered.values()
            if query in (spec['name']+' '+spec.get('description','')).lower()]
    offset = max(0,args.get('offset',0))
    selected, size = [], 0
    for spec in rows[offset:offset+5]:
        width = len(json.dumps(spec))
        if size+width > 11000:
            if not selected:
                return json.dumps(dict(error='Tool schema exceeds display budget; inspect the installed CLI help.',name=spec['name']))
            break
        selected.append(spec);size+=width
    return json.dumps(dict(app=args['app'],total=len(rows),offset=offset,
                           next_offset=offset+len(selected) if offset+len(selected)<len(rows) else None,
                           tools=selected))


def call(ctx, args):
    b = bus(ctx,args['app'])
    name = tool_name(args['app'],args['tool'])
    if name not in b.discovered:
        raise ToolError('Unknown app tool. Discover its exact name and schema using creative_tools first.')
    return b.call(name,args.get('arguments',{}))


def blender_run(ctx, args):
    binary = executable('blender')
    if not binary:
        raise ToolError('Blender is not installed or not on PATH.')
    script = ctx.ws.path(args['script'])
    if script.suffix != '.py' or not script.is_file():
        raise ToolError('script must be an existing Python file in the project.')
    argv = [binary,'--background','--factory-startup','--disable-autoexec','--threads','2',
            '--python-exit-code','1','--python',str(script)]
    timeout = max(1,min(600,args.get('timeout',120)))
    code, output, timed_out = run_command(shlex.join(argv),ctx.ws.repo,timeout=timeout)
    payload = json.dumps(dict(status='timeout' if timed_out else 'passed' if code==0 else 'failed',
                              exit_code=code,output=output))
    # Match the controller receipt protocol: partial render output is not success.
    return ('TIMEOUT: ' if timed_out else 'ERROR: ' if code != 0 else '') + payload


def media_probe(ctx,args):
    binary = executable('ffprobe')
    if not binary:
        raise ToolError('ffprobe is not installed.')
    path = ctx.ws.path(args['path'])
    if not path.is_file():
        raise ToolError('Media file not found.')
    result = subprocess.run([binary,'-v','error','-show_format','-show_streams','-of','json',str(path)],
                            cwd=ctx.ws.repo,capture_output=True,text=True,timeout=20)
    if result.returncode:
        raise ToolError(result.stderr[:1000])
    return result.stdout[:10000]
