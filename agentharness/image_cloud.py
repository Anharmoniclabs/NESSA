"""Opt-in HF/fal image generation; no SDK, hidden retries, or token-bearing logs."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .cloud import HF_ROUTER
from .workspace import ToolError

MODELS = {'black-forest-labs/FLUX.1-dev': 'fal-ai/flux/dev',
          'black-forest-labs/FLUX.1-schnell': 'fal-ai/flux/schnell',
          'Qwen/Qwen-Image-2512': 'fal-ai/qwen-image-2512',
          'Qwen/Qwen-Image': 'fal-ai/qwen-image'}
DEFAULT_MODEL = 'black-forest-labs/FLUX.1-dev'
MODEL_STEPS = {'black-forest-labs/FLUX.1-dev': 28,
               'black-forest-labs/FLUX.1-schnell': 4,
               'Qwen/Qwen-Image-2512': 50, 'Qwen/Qwen-Image': 50}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ToolError('Image service redirected unexpectedly; request stopped.')


def _fetch(url, *, token=None, payload=None, limit=1_000_000, timeout=240):
    headers = {'Accept': 'application/json' if payload is not None else '*/*'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ToolError('Image service response exceeds size limit.')
    return data


def generate(ctx, args):
    # Token existence is not consent. Only explicitly configured HF chat clients
    # grant this capability, including when the chat has fallen back locally.
    client = getattr(ctx, 'client', None)
    clients = getattr(client, 'clients', [client])
    token = next((getattr(c, 'api_key', '') for c in clients
                  if getattr(c, 'base_url', '').rstrip('/') == HF_ROUTER), '')
    if not token.startswith('hf_'):
        raise ToolError('Cloud image generation requires a Hugging Face cloud session (--cloud). No local image engine is configured.')
    model = args.get('model', DEFAULT_MODEL)
    if model not in MODELS:
        raise ToolError('Unsupported image model; choose ' + ', '.join(MODELS))
    prompt = args['prompt'].strip()
    if not 1 <= len(prompt) <= 6000:
        raise ToolError('Image prompt must be 1–6000 characters.')
    width, height = args.get('width', 1664), args.get('height', 928)
    seed = args.get('seed', 42)
    if any(type(n) is not int or not 512 <= n <= 2048 or n % 32 for n in (width, height)):
        raise ToolError('Image dimensions must be multiples of 32, from 512 to 2048.')
    if width * height > 4_000_000 or type(seed) is not int or not 0 <= seed < 2**31:
        raise ToolError('Image too large or seed outside 0..2147483647.')
    dest = ctx.ws.path(args['path'])
    if dest.suffix.lower() != '.png':
        raise ToolError('Image output path must end in .png.')
    receipt = dest.with_suffix('.generation.json')
    if dest.exists() or receipt.exists():
        raise ToolError('Output or generation receipt already exists; choose a new versioned path.')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ToolError('Install ffmpeg to validate generated image decoding.')
    payload = dict(prompt=prompt, image_size=dict(width=width, height=height),
                   seed=seed, num_inference_steps=MODEL_STEPS[model], num_images=1, output_format='png')
    dest.parent.mkdir(parents=True, exist_ok=True)
    record = dict(status='started', model=model, provider='fal-ai', transport='huggingface-router',
                  parameters=payload, path=str(dest), started_at=time.time(), billed_cost=None)
    # Reserve once. An interrupted request may have been billed; never auto retry.
    with receipt.open('x') as f:
        json.dump(record, f, indent=2)
    start = time.monotonic()
    try:
        result = json.loads(_fetch('https://router.huggingface.co/fal-ai/' + MODELS[model],
                                  token=token, payload=payload))
        url = result['images'][0]['url']
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.port not in (None, 443) or not (parsed.hostname == 'fal.media' or
                (parsed.hostname or '').endswith('.fal.media'))):
            raise ToolError('Unrecognized image download host; artifact download stopped.')
        data = _fetch(url, limit=20_000_000)  # Never forward HF credentials to CDN.
        if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
            raise ToolError('Provider did not return a PNG image.')
        actual = struct.unpack('>II', data[16:24])
        if actual != (width, height):
            raise ToolError('Provider returned unexpected image dimensions.')
        decoded = subprocess.run([ffmpeg, '-v', 'error', '-xerror', '-i', 'pipe:0',
                                  '-f', 'null', '-'], input=data, capture_output=True, timeout=30)
        if decoded.returncode:
            raise ToolError('Generated image failed full decoding validation.')
        with dest.open('xb') as f:
            f.write(data)
        record.update(status='generated', sha256=hashlib.sha256(data).hexdigest(),
                      bytes=len(data), actual_dimensions=list(actual),
                      returned_seed=result.get('seed'), review='not yet visually reviewed')
    except BaseException as exc:
        # Provider exception bodies may contain credentials/URLs; never echo them.
        record['status'] = 'outcome_unknown' if isinstance(exc, (KeyboardInterrupt, TimeoutError)) else 'failed'
        record['error_type'] = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            record['http_status'] = exc.code
        if isinstance(exc, KeyboardInterrupt):
            raise
        detail = f' HTTP {exc.code}.' if isinstance(exc, urllib.error.HTTPError) else ''
        raise ToolError(f'Image generation failed ({type(exc).__name__}).{detail} Inspect {receipt.name}; no automatic retry.') from None
    finally:
        record['seconds'] = time.monotonic() - start
        receipt.write_text(json.dumps(record, indent=2) + '\n')
    return json.dumps({k: v for k, v in record.items() if k != 'parameters'})[:4000]
