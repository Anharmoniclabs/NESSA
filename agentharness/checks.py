"""Bounded command execution and honest check results.

Every check ends in exactly one status, so failures are never reported as success:
    passed | failed | timeout | setup_error | no_tests | error
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

OUTPUT_TAIL = 8000


@dataclass
class CheckResult:
    name: str
    status: str
    command: str = ""
    exit_code: int | None = None
    seconds: float = 0.0
    counts: dict = field(default_factory=dict)
    output: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def brief(self) -> str:
        counts = " ".join(f"{k}={v}" for k, v in self.counts.items())
        return f"[{self.name}] {self.status} (exit={self.exit_code}, {self.seconds:.1f}s) {counts}".rstrip()


def run_command(command: str, cwd: Path, timeout: float = 300, output_limit: int = OUTPUT_TAIL,
                env: dict | None = None) -> tuple[int | None, str, bool]:
    """Run a shell command in its own process group. Returns (exit_code, output_tail, timed_out)."""
    full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", **(env or {})}
    with tempfile.TemporaryFile() as buf:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=buf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=full_env,
                                start_new_session=(os.name == "posix"))
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait()
        size = buf.seek(0, 2)
        buf.seek(max(0, size - output_limit))
        tail = buf.read().decode("utf-8", "replace")
        if size > output_limit:
            tail = f"[... {size - output_limit} bytes of earlier output omitted ...]\n" + tail
    return (None if timed_out else proc.returncode), tail, timed_out


def parse_counts(output: str) -> dict:
    counts = {}
    # pytest summary: "3 failed, 10 passed, 1 error in 0.5s"
    for key in ("passed", "failed", "errors", "error", "skipped", "xfailed"):
        m = re.findall(rf"(\d+) {key}\b", output)
        if m:
            counts["errors" if key == "error" else key] = int(m[-1])
    # unittest: "Ran 5 tests" / "FAILED (failures=1, errors=2)"
    if m := re.findall(r"Ran (\d+) tests?", output):
        ran = int(m[-1])
        fails = sum(int(x) for x in re.findall(r"failures=(\d+)", output)[-1:])
        errs = sum(int(x) for x in re.findall(r"errors=(\d+)", output)[-1:])
        counts.setdefault("failed", fails)
        counts.setdefault("errors", errs)
        counts.setdefault("passed", max(0, ran - fails - errs))
    return counts


def classify(command: str, exit_code: int | None, output: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    low = output.lower()
    if exit_code == 127 or "command not found" in low or "no module named pytest" in low:
        return "setup_error"
    if "pytest" in command:
        return {0: "passed", 1: "failed", 5: "no_tests"}.get(exit_code, "setup_error")
    if re.search(r"\bRan 0 tests\b", output) or "NO TESTS RAN" in output:
        return "no_tests"
    if exit_code == 0:
        return "passed"
    if "error while importing test module" in low or "error collecting" in low:
        return "setup_error"
    return "failed"


Check = Callable[["object"], CheckResult] | str  # shell command or callable(workspace)


def syntax_check(ws) -> CheckResult:
    """Parse every changed .py/.json file. Fast and precise; needs no project setup."""
    started, problems, n = time.monotonic(), [], 0
    for rel in ws.changed_files():
        p = ws.repo / rel
        if not p.is_file():
            continue
        try:
            if rel.endswith(".py"):
                n += 1
                compile(p.read_text(encoding="utf-8", errors="replace"), rel, "exec")
            elif rel.endswith(".json"):
                n += 1
                json.loads(p.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError) as exc:
            problems.append(f"{rel}: {exc}")
    status = "failed" if problems else ("passed" if n else "no_tests")
    return CheckResult("syntax", status, "compile changed files", 1 if problems else 0,
                       time.monotonic() - started, {"files": n, "failed": len(problems)},
                       "\n".join(problems) or f"{n} changed files parse cleanly")


def detect_checks(repo: Path) -> dict[str, Check]:
    """Default checks: syntax always; tests when the project appears to have Python tests."""
    checks: dict[str, Check] = {"syntax": syntax_check}
    has_tests = any(repo.glob("test*/**/*.py")) or any(repo.glob("**/test_*.py")) \
        or any(repo.glob("**/*_test.py"))
    if has_tests:
        py = f'"{sys.executable}"'
        if importlib.util.find_spec("pytest"):
            checks["tests"] = f"{py} -m pytest -q -p no:cacheprovider"
        else:
            checks["tests"] = f"{py} -m unittest discover -q"
    return checks


class CheckRunner:
    def __init__(self, checks: dict[str, Check], timeout: float = 600):
        self.checks = dict(checks)
        self.timeout = timeout

    def names(self) -> list[str]:
        return list(self.checks)

    def run(self, name: str, ws, extra_args: str = "") -> CheckResult:
        check = self.checks.get(name)
        if check is None:
            return CheckResult(name, "error", output=f"Unknown check {name!r}; available: {self.names()}")
        if callable(check):
            return check(ws)
        command = f"{check} {extra_args}".strip()
        started = time.monotonic()
        code, out, timed_out = run_command(command, ws.repo, self.timeout)
        return CheckResult(name, classify(command, code, out, timed_out), command, code,
                           time.monotonic() - started, parse_counts(out), out)
