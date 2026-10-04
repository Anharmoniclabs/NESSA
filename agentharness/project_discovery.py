"""Bounded, read-only project discovery without a model round trip."""
import difflib
import re
import tomllib
from pathlib import Path


def find_project(message, roots):
    candidates = []
    for root in roots:
        root = Path(root).expanduser()
        if root.is_dir():
            candidates.extend(p for p in sorted(root.iterdir())
                              if p.is_dir() and not p.is_symlink() and not p.name.startswith('.'))
    candidates = candidates[:200]
    exact = [p for p in candidates if str(p) in message]
    if exact:
        return exact
    if not re.search(r'\b(inspect|review|open|check|look|explore)\b', message, re.I):
        return []
    words = re.findall(r'[a-z0-9]+', message.lower())
    phrases = [''.join(words[i:i+n]) for i in range(len(words)) for n in range(1, 5)]
    matches = []
    for p in candidates:
        name = re.sub(r'[^a-z0-9]', '', p.name.lower())
        score = max((difflib.SequenceMatcher(None, name, s).ratio() for s in phrases), default=0)
        if score >= .82:
            matches.append(p)
    return matches


def overview(project):
    project = Path(project)
    visible = [p for p in sorted(project.iterdir()) if not p.name.startswith('.')]
    lines = [f'Found {project.name} at {project}.', '', 'Initial filesystem inspection:']
    manifest = project / 'pyproject.toml'
    if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size < 100_000:
        try:
            data = tomllib.loads(manifest.read_text()).get('project', {})
            lines.append(f"Python project: {data.get('name', project.name)}; version {data.get('version', 'not specified')}.")
            if data.get('description'):
                lines.append(str(data['description'])[:300])
        except (OSError, ValueError):
            pass
    lines.append('Top-level files and folders: ' + ', '.join(p.name + ('/' if p.is_dir() else '') for p in visible[:30]))
    lines.append('This is a directory and metadata inspection; I have not run the app or its tests.')
    return '\n'.join(lines)
