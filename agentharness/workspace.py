"""Private project snapshot the agent edits. The user's original files are never touched.

Layout of a work directory:
    baseline/  untouched copy used for diffs and undo
    repo/      the copy the agent reads, edits and tests
The final result is a unified diff (baseline -> repo) that applies with `git apply`.

Direct mode (Workspace.direct) edits the user's project in place, like enterprise CLI agents:
    repo      is the user's project itself
    baseline/ is a snapshot taken when the session started, kept for diffs, undo and evidence
"""
from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

COPY_IGNORE = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SCAN_SKIP = COPY_IGNORE | {".venv", "venv", "env", "node_modules", ".tox", ".agentharness",
                           "dist", "build", ".idea", ".vscode"}
JUNK_SUFFIXES = (".pyc", ".pyo", ".so", ".o", ".egg-info")
MAX_TEXT_BYTES = 1_000_000
READ_PAGE_LINES = 200
READ_MAX_CHARS = 12_000


class ToolError(Exception):
    """An error message meant for the model, not a crash."""


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:12]


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def iter_files(root: Path):
    """Yield (relative posix path, absolute path) for project files, skipping caches/venvs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SCAN_SKIP and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if name.endswith(JUNK_SUFFIXES):
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            yield p.relative_to(root).as_posix(), p


class Workspace:
    def __init__(self, work_dir: Path, repo: Path | None = None):
        self.work_dir = Path(work_dir).resolve()
        self.repo = Path(repo).resolve() if repo else self.work_dir / "repo"
        self.direct = repo is not None
        self.baseline = self.work_dir / "baseline"
        self.journal: list[dict] = []
        self._seen: dict[str, str] = {}  # rel path -> sha of the version the agent last read

    @classmethod
    def create(cls, source: Path, work_dir: Path) -> "Workspace":
        source, work_dir = Path(source).resolve(), Path(work_dir).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Project folder not found: {source}")
        if work_dir == source or source in work_dir.parents:
            raise ValueError("The work directory must be outside the project folder.")
        ws = cls(work_dir)
        if any((work_dir / name).exists() for name in ('repo', 'baseline', 'evidence')):
            raise FileExistsError(f'Work directory is not empty: {work_dir}; use resume or a new directory')
        ignore = shutil.ignore_patterns(*COPY_IGNORE)
        shutil.copytree(source, ws.repo, symlinks=True, ignore=ignore)
        shutil.copytree(source, ws.baseline, symlinks=True,
                        ignore=shutil.ignore_patterns(*SCAN_SKIP, "*.pyc"))
        return ws

    @classmethod
    def direct(cls, source: Path, work_dir: Path) -> "Workspace":
        """Edit `source` in place; only the baseline snapshot lives in `work_dir`."""
        source, work_dir = Path(source).resolve(), Path(work_dir).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Project folder not found: {source}")
        if work_dir == source or source in work_dir.parents:
            raise ValueError("The work directory must be outside the project folder.")
        if any((work_dir / name).exists() for name in ('repo', 'baseline', 'evidence')):
            raise FileExistsError(f'Work directory is not empty: {work_dir}; use resume or a new directory')
        ws = cls(work_dir, repo=source)
        shutil.copytree(source, ws.baseline, symlinks=True,
                        ignore=shutil.ignore_patterns(*SCAN_SKIP, "*.pyc"))
        (work_dir / "DIRECT").write_text(str(source))
        return ws

    @classmethod
    def open(cls, work_dir: Path) -> "Workspace":
        """Reopen a private or direct workspace for resume."""
        work_dir = Path(work_dir).resolve()
        marker = work_dir / "DIRECT"
        return cls(work_dir, repo=Path(marker.read_text().strip())) if marker.is_file() else cls(work_dir)

    # ------------------------------------------------------------ paths

    def path(self, rel: str | None) -> Path:
        rel = (rel or ".").strip() or "."
        p = (self.repo / rel).resolve()
        if p != self.repo and self.repo not in p.parents:
            raise ToolError(f"Path is outside the project: {rel}")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.repo).as_posix() if p != self.repo else "."

    def _text(self, p: Path) -> str:
        if not p.is_file():
            raise ToolError(f"Not a file: {self.rel(p)}")
        data = p.read_bytes()
        if _is_binary(data):
            raise ToolError(f"Binary file: {self.rel(p)}")
        if len(data) > MAX_TEXT_BYTES:
            raise ToolError(f"File too large to read ({len(data)} bytes): {self.rel(p)}")
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------ read tools

    def list_dir(self, rel: str = ".", depth: int = 1) -> str:
        root = self.path(rel)
        if not root.is_dir():
            raise ToolError(f"Not a directory: {rel}")
        out: list[str] = []

        def walk(d: Path, level: int):
            for p in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
                if p.name in SCAN_SKIP or p.name.endswith(JUNK_SUFFIXES):
                    continue
                out.append("  " * level + p.name + ("/" if p.is_dir() else ""))
                if p.is_dir() and level + 1 < depth:
                    walk(p, level + 1)
                if len(out) >= 400:
                    return
        walk(root, 0)
        return "\n".join(out[:400]) or "(empty)"

    def read(self, rel: str, start: int = 1, end: int | None = None) -> str:
        p = self.path(rel)
        text = self._text(p)
        lines = text.splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end) if end else start + READ_PAGE_LINES - 1)
        body, size = [], 0
        for i in range(start, end + 1):
            line = f"{i:>5} {lines[i - 1]}"
            size += len(line) + 1
            if size > READ_MAX_CHARS:
                end = i - 1
                break
            body.append(line)
        self._seen[self.rel(p)] = sha(text)
        more = f"; next: start={end + 1}" if end < len(lines) else ""
        return f"{self.rel(p)} lines {start}-{end} of {len(lines)}{more}\n" + "\n".join(body)

    def search(self, pattern: str, rel: str = ".", glob: str | None = None,
               max_hits: int = 100, ignore_case: bool = False) -> str:
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as exc:
            raise ToolError(f"Bad regex: {exc}")
        base = self.path(rel)
        files = [(self.rel(base), base)] if base.is_file() else \
            [(self.rel(p), p) for _, p in iter_files(base)]
        hits, total = [], 0
        for r, p in files:
            if glob and not (fnmatch.fnmatch(p.name, glob) or fnmatch.fnmatch(r, glob)):
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if len(data) > MAX_TEXT_BYTES or _is_binary(data):
                continue
            per_file = 0
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    total += 1
                    if len(hits) < max_hits and per_file < 20:
                        hits.append(f"{r}:{n}: {line.strip()[:200]}")
                        per_file += 1
        if not hits:
            return "no matches"
        extra = f"\n... {total - len(hits)} more matches not shown; narrow the search" if total > len(hits) else ""
        return "\n".join(hits) + extra

    def outline(self, rel: str) -> str:
        p = self.path(rel)
        text = self._text(p)
        out: list[str] = []
        if p.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                out.append(f"(syntax error line {exc.lineno}: {exc.msg}; regex outline follows)")
            else:
                def visit(node, level):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            kind = "class" if isinstance(child, ast.ClassDef) else "def"
                            out.append(f"{'  ' * level}{kind} {child.name}  "
                                       f"lines {child.lineno}-{child.end_lineno}")
                            visit(child, level + 1)
                visit(tree, 0)
                return f"{self.rel(p)}\n" + ("\n".join(out) or "(no functions or classes)")
        decl = re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?(pub\s+)?"
                          r"(def|class|function|fn|func|interface|struct|impl|enum|type)\b")
        for n, line in enumerate(text.splitlines(), 1):
            if decl.match(line):
                out.append(f"{n:>5} {line.strip()[:160]}")
        return f"{self.rel(p)}\n" + ("\n".join(out[:300]) or "(no declarations found)")

    # ------------------------------------------------------------ edit tools

    def _require_fresh(self, p: Path, text: str):
        r = self.rel(p)
        if r not in self._seen:
            raise ToolError(f"Read {r} before editing it.")
        if self._seen[r] != sha(text):
            raise ToolError(f"{r} changed since you last read it. Re-read it, then edit.")

    def _commit(self, p: Path, before: str | None, after: str, op: str):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(after, encoding="utf-8")
        r = self.rel(p)
        self._seen[r] = sha(after)
        self.journal.append({"t": time.time(), "op": op, "path": r,
                             "before": None if before is None else sha(before), "after": sha(after)})

    def replace(self, rel: str, old: str, new: str) -> str:
        p = self.path(rel)
        text = self._text(p)
        self._require_fresh(p, text)
        if not old:
            raise ToolError("`old` must not be empty.")
        n = text.count(old)
        if n == 1:
            self._commit(p, text, text.replace(old, new, 1), "replace")
            return f"ok: replaced 1 occurrence in {self.rel(p)}"
        if n > 1:
            raise ToolError(f"`old` occurs {n} times; include more surrounding lines so it is unique.")
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        lines = text.splitlines()
        close = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.6)
        hint = ""
        if close:
            idx = [l.strip() for l in lines].index(close[0]) + 1
            hint = f" Closest line {idx}: {lines[idx - 1].strip()[:160]!r}. Re-read and copy text exactly (whitespace matters)."
        raise ToolError("`old` text not found." + hint)

    def edit_lines(self, rel: str, start: int, end: int, new: str) -> str:
        p = self.path(rel)
        text = self._text(p)
        self._require_fresh(p, text)
        lines = text.splitlines(keepends=True)
        start, end = int(start), int(end)
        if not (1 <= start <= len(lines) + 1) or end < start - 1 or end > len(lines):
            raise ToolError(f"Line range {start}-{end} invalid; file has {len(lines)} lines.")
        repl = new if new.endswith("\n") or not new else new + "\n"
        after = "".join(lines[:start - 1]) + repl + "".join(lines[end:])
        self._commit(p, text, after, "edit_lines")
        return f"ok: lines {start}-{end} of {self.rel(p)} replaced with {len(repl.splitlines())} lines"

    def write(self, rel: str, content: str) -> str:
        p = self.path(rel)
        before = None
        if p.exists():
            before = self._text(p)
            self._require_fresh(p, before)
        self._commit(p, before, content, "write")
        return f"ok: wrote {len(content)} chars to {self.rel(p)}"

    def undo(self, rel: str) -> str:
        p = self.path(rel)
        r = self.rel(p)
        base = self.baseline / r
        if base.is_file():
            shutil.copy2(base, p)
            self._seen.pop(r, None)
            self.journal.append({"t": time.time(), "op": "undo", "path": r})
            return f"ok: {r} restored to original"
        if p.exists():
            p.unlink()
            self.journal.append({"t": time.time(), "op": "undo", "path": r})
            return f"ok: new file {r} removed"
        return f"{r} has no changes"

    # ------------------------------------------------------------ results

    def changed_files(self) -> list[str]:
        a = {r: p for r, p in iter_files(self.baseline)}
        b = {r: p for r, p in iter_files(self.repo)}
        return [r for r in sorted(a.keys() | b.keys())
                if r not in a or r not in b or a[r].read_bytes() != b[r].read_bytes()]

    def apply_to(self, project: Path) -> list[str]:
        """Copy changed files into the original project after an explicit user decision.

        Refuses (changing nothing) when any target differs from the snapshot baseline, so
        edits the user made after the copy are never overwritten. Applied files become the
        new baseline: a later patch contains only newer work.
        """
        project = Path(project).resolve()
        if self.direct and project == self.repo:
            return []  # edits are already in the project
        if not project.is_dir():
            raise ToolError(f"Project folder not found: {project}")
        changed = self.changed_files()
        conflicts = []
        for r in changed:
            target, base = project / r, self.baseline / r
            if target.is_symlink() or project not in target.resolve().parents:
                conflicts.append(f"{r} (outside the project or a symlink)")
            elif base.is_file() != target.is_file() or (
                    base.is_file() and base.read_bytes() != target.read_bytes()):
                conflicts.append(f"{r} (changed in the project since it was copied)")
        if conflicts:
            raise ToolError("Not applied; no files were changed. Conflicts: " + "; ".join(conflicts))
        for r in changed:
            src, target, base = self.repo / r, project / r, self.baseline / r
            if src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".nessa-tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, target)
                base.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, base)
            else:
                target.unlink(missing_ok=True)
                base.unlink(missing_ok=True)
        return changed

    def patch(self) -> str:
        out = []
        for r in self.changed_files():
            a, b = self.baseline / r, self.repo / r
            da = a.read_bytes() if a.is_file() else b""
            db = b.read_bytes() if b.is_file() else b""
            header = f"diff --git a/{r} b/{r}\n"
            if not a.is_file():
                header += "new file mode 100644\n"
            elif not b.is_file():
                header += "deleted file mode 100644\n"
            if _is_binary(da) or _is_binary(db):
                out.append(header + f"Binary files a/{r} and b/{r} differ\n")
                continue
            la = da.decode("utf-8", "replace").splitlines(keepends=True)
            lb = db.decode("utf-8", "replace").splitlines(keepends=True)
            diff = difflib.unified_diff(la, lb, "a/" + r if a.is_file() else "/dev/null",
                                        "b/" + r if b.is_file() else "/dev/null")
            body = []
            for line in diff:
                if line.endswith("\n"):
                    body.append(line)
                else:
                    body.append(line + "\n\\ No newline at end of file\n")
            out.append(header + "".join(body))
        return "".join(out)
