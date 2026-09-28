"""Persistent project instructions for the agent.

Supports the open AGENTS.md convention plus local agent.md / .agent/instructions.md files.
Root instructions are packed into the initial context. Directory-scoped instructions can
be queried for a target file without loading every instruction file in the repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT_NAMES = ("AGENTS.md", "agent.md", "AGENT.md")
EXTRA_ROOT = (".agent/instructions.md",)


@dataclass(frozen=True)
class InstructionFile:
    path: str
    text: str


class ProjectInstructions:
    def __init__(self, root: Path, max_chars: int = 12000):
        self.root = Path(root).resolve()
        self.max_chars = max(1000, int(max_chars))

    def _read(self, path: Path) -> InstructionFile | None:
        try:
            path = path.resolve()
            path.relative_to(self.root)
        except (ValueError, OSError):
            return None
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text:
            return None
        return InstructionFile(path.relative_to(self.root).as_posix(), text)

    def root_files(self) -> list[InstructionFile]:
        found: list[InstructionFile] = []
        for name in ROOT_NAMES:
            item = self._read(self.root / name)
            if item:
                found.append(item)
        for rel in EXTRA_ROOT:
            item = self._read(self.root / rel)
            if item:
                found.append(item)
        return found

    def for_path(self, rel: str) -> list[InstructionFile]:
        """Return root + nested instruction files in broad-to-specific order."""
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            return []
        directory = target if target.is_dir() else target.parent
        lineage = [self.root]
        if directory != self.root:
            cur = self.root
            for part in directory.relative_to(self.root).parts:
                cur = cur / part
                lineage.append(cur)
        found: list[InstructionFile] = []
        seen: set[str] = set()
        for directory in lineage:
            names = ROOT_NAMES if directory == self.root else ("AGENTS.md", "agent.md", "AGENT.md")
            for name in names:
                item = self._read(directory / name)
                if item and item.path not in seen:
                    found.append(item)
                    seen.add(item.path)
        if self.root == directory:
            for rel_extra in EXTRA_ROOT:
                item = self._read(self.root / rel_extra)
                if item and item.path not in seen:
                    found.append(item)
                    seen.add(item.path)
        return found

    def render(self, files: list[InstructionFile]) -> str:
        if not files:
            return ""
        chunks: list[str] = []
        used = 0
        for item in files:
            header = f"### {item.path}\n"
            room = self.max_chars - used - len(header)
            if room <= 0:
                break
            body = item.text[:room]
            chunks.append(header + body)
            used += len(header) + len(body)
        return "\n\n".join(chunks)

    def root_context(self) -> str:
        return self.render(self.root_files())

    def context_for(self, rel: str) -> str:
        return self.render(self.for_path(rel))
