"""Local AnharmonicStudio production adapter (no model-generated shell commands)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

from .workspace import ToolError


def socket_path():
    return Path.home() / '.local/state/anharmonic-studio/control.sock'


def _connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    return sock


def command(args, *, timeout=45):
    """Launch only before sending; never replay a command with an unknown outcome."""
    path = socket_path()
    try:
        sock = _connect(path)
    except OSError:
        root = Path(os.environ.get('NESSA_STUDIO_ROOT', str(Path.home() / 'Projects/AnharmonicStudio')))
        launcher = root / 'run.sh'
        if not launcher.is_file():
            raise ToolError('Studio installation not found. Set NESSA_STUDIO_ROOT to its checkout.')
        logs = Path.home() / '.local/state/nessa'
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / 'studio-launch.log').open('ab') as log:
            subprocess.Popen(['bash', str(launcher)], cwd=root, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                sock = _connect(path)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ToolError('Studio control did not become ready. Restart Studio to load the new bridge; '
                                    'launch details are in ~/.local/state/nessa/studio-launch.log.')
                time.sleep(.2)
    with sock:
        try:
            sock.sendall(json.dumps(args).encode() + b'\n')
            data = b''
            while b'\n' not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError('Studio disconnected before reporting a result')
                data += chunk
                if len(data) > 8192:
                    raise OSError('Studio response exceeded the limit')
            result = json.loads(data.split(b'\n', 1)[0])
        except (OSError, ValueError) as exc:
            return f'TIMEOUT: Studio outcome unknown ({exc}). Inspect Studio before repeating the action.'
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ToolError(str(result.get('error', 'Invalid Studio result'))[:500]
                        if isinstance(result, dict) else 'Invalid Studio result')
    return json.dumps(result)[:4000]


def quick_request(message, *, studio_context=False):
    """Conservative shortcuts for explicit, simple production requests only.

    Complex requests go to the model with the structured studio_control tool.
    Never extract a command from a quotation, negation or code-editing request.
    """
    text = message.lower().strip().rstrip('.!?')
    if re.search(r"\b(don't|dont|not|never|code|implement|train|fix|why|explain|said)\b|[\"`\n]", text):
        return None
    named = bool(re.search(r'anharmonic\s*studio', text))
    text = re.sub(r'^(?:please\s+|can you\s+|could you\s+|would you\s+)', '', text)
    text = re.sub(r'^go into (?:my |the )?anharmonic\s*studio and ', '', text)
    text = re.sub(r' (?:in|on|with) (?:my |the )?anharmonic\s*studio$', '', text)
    match = re.fullmatch(r'(?:make|create|build) (?:me )?(?:a |an )?(?:(pocket|circuit|midnight|trap) )?beat(?: at (\d+(?:\.\d+)?) bpm)?', text)
    if match:
        kits = {'pocket': 'Pocket', 'circuit': 'Circuit', 'midnight': 'Midnight', 'trap': 'Trap Foundry'}
        args = {'action': 'make_beat', 'kit': kits.get(match[1], 'Pocket')}
        if match[2]:
            args['bpm'] = float(match[2])
        return args
    if re.fullmatch(r'open (?:my |the )?anharmonic\s*studio', text):
        return {'action': 'open'}
    if named or studio_context:
        if text in ('play', 'play it', 'play the beat', 'stop', 'stop playback'):
            return {'action': 'stop' if text.startswith('stop') else 'play'}
        match = re.fullmatch(r'(?:set|change) (?:the )?tempo to (\d+(?:\.\d+)?)(?: bpm)?', text)
        if match:
            return {'action': 'set_tempo', 'bpm': float(match[1])}
    return None


def production_request(message):
    """Keep named music requests out of the source-code project discovery shortcut."""
    return bool(re.search(r'anharmonic\s*studio', message, re.I)
                and re.search(r'\b(beat|drums|tempo|bpm|play|playback|music)\b', message, re.I)
                and not re.search(r'\b(code|implement|fix|debug|train|test)\b', message, re.I))


def describe(output):
    if output.startswith('TIMEOUT'):
        return output
    result = json.loads(output)
    action = result['action']
    if action == 'make_beat':
        return (f"Created {result['pattern']} at {result['bpm']:g} BPM in AnharmonicStudio. "
                'The drums are editable in the step sequencer. Say “play it” to listen; Undo removes the beat.')
    if action == 'open':
        return 'AnharmonicStudio is open and connected.'
    if action == 'set_tempo':
        return f"Set Studio to {result['bpm']:g} BPM."
    return f"Studio: {result['pattern']}, {result['bpm']:g} BPM, {'playing' if result['playing'] else 'stopped'}."
