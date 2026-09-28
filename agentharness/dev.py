"""Managed local development processes for skills and debugging.

This is a developer-experience harness, not a security sandbox. Commands are argv arrays
(no shell expansion), logs are captured under the run evidence directory, and the parent
agent can start/status/tail/stop named processes without nesting another agent.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DevProcess:
    name: str
    argv: list[str]
    cwd: str
    pid: int
    log_path: str
    started_at: float
    proc: subprocess.Popen


class DevProcessManager:
    def __init__(self, workspace, evidence_dir: Path):
        self.workspace = workspace
        self.dir = Path(evidence_dir) / "dev"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.processes: dict[str, DevProcess] = {}

    @staticmethod
    def _name(name: str) -> str:
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("process name must match [A-Za-z0-9_.-]{1,64}")
        return name

    def start(self, name: str, argv: list[str], cwd: str = ".") -> str:
        name = self._name(name)
        if name in self.processes and self.processes[name].proc.poll() is None:
            raise ValueError(f"dev process {name!r} is already running")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("argv must be a non-empty array of strings")
        workdir = self.workspace.path(cwd)
        if not workdir.is_dir():
            raise ValueError(f"dev cwd is not a directory: {cwd}")
        log_path = self.dir / f"{name}.log"
        log = log_path.open("ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            log.close()
        time.sleep(0.15)
        item = DevProcess(name, list(argv), self.workspace.rel(workdir), proc.pid,
                          str(log_path), time.time(), proc)
        self.processes[name] = item
        self._write_state()
        if proc.poll() is not None:
            return f"{name}: exited immediately with code {proc.returncode}\n{self.logs(name)}"
        return f"{name}: running pid={proc.pid} cwd={item.cwd} log={log_path}"

    def status(self, name: str | None = None) -> str:
        names = [self._name(name)] if name else sorted(self.processes)
        if not names:
            return "(no managed dev processes)"
        rows = []
        for key in names:
            item = self.processes.get(key)
            if not item:
                rows.append(f"{key}: unknown")
                continue
            code = item.proc.poll()
            state = "running" if code is None else f"exited({code})"
            rows.append(f"{key}: {state} pid={item.pid} cwd={item.cwd}")
        self._write_state()
        return "\n".join(rows)

    def logs(self, name: str, max_bytes: int = 12000) -> str:
        name = self._name(name)
        item = self.processes.get(name)
        if not item:
            return f"{name}: unknown"
        path = Path(item.log_path)
        if not path.exists():
            return "(no log yet)"
        data = path.read_bytes()
        clipped = data[-max(1000, int(max_bytes)):]
        prefix = f"[... {len(data) - len(clipped)} earlier bytes omitted ...]\n" if len(clipped) < len(data) else ""
        return prefix + clipped.decode("utf-8", errors="replace")

    def stop(self, name: str, grace_seconds: float = 2.0) -> str:
        name = self._name(name)
        item = self.processes.get(name)
        if not item:
            return f"{name}: unknown"
        if item.proc.poll() is not None:
            return f"{name}: already exited({item.proc.returncode})"
        try:
            if os.name == "posix":
                os.killpg(item.proc.pid, signal.SIGTERM)
            else:
                item.proc.terminate()
            item.proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(item.proc.pid, signal.SIGKILL)
            else:
                item.proc.kill()
            item.proc.wait()
        self._write_state()
        return f"{name}: stopped exit={item.proc.returncode}"

    def stop_all(self) -> None:
        for name in list(self.processes):
            try:
                self.stop(name)
            except Exception:
                pass

    def _write_state(self) -> None:
        rows = []
        for item in self.processes.values():
            rows.append({
                "name": item.name,
                "argv": item.argv,
                "cwd": item.cwd,
                "pid": item.pid,
                "log_path": item.log_path,
                "started_at": item.started_at,
                "returncode": item.proc.poll(),
            })
        (self.dir / "processes.json").write_text(json.dumps(rows, indent=2))
