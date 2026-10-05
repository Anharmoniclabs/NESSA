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

from .session import atomic_json


@dataclass
class DevProcess:
    name: str
    argv: list[str]
    cwd: str
    pid: int
    log_path: str
    started_at: float
    proc: subprocess.Popen | None
    identity: str | None = None
    returncode: int | None = None


class DevProcessManager:
    def __init__(self, workspace, evidence_dir: Path):
        self.workspace = workspace
        self.dir = Path(evidence_dir) / "dev"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.processes: dict[str, DevProcess] = {}
        self.state_path = self.dir / "processes.json"
        self._reconcile()

    @staticmethod
    def _name(name: str) -> str:
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("process name must match [A-Za-z0-9_.-]{1,64}")
        return name

    @staticmethod
    def _identity(pid: int) -> str | None:
        """Linux process start-time identity used to reject PID reuse."""
        if os.name != "posix":
            return None
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            return fields[21] if len(fields) > 21 else None
        except OSError:
            return None

    @classmethod
    def _alive(cls, pid: int, identity: str | None) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        current = cls._identity(pid)
        return identity is None or current is None or current == identity

    def _reconcile(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        rows = payload.get("processes", []) if isinstance(payload, dict) else payload
        for row in rows:
            try:
                item = DevProcess(row["name"], list(row["argv"]), row["cwd"], int(row["pid"]),
                                  row["log_path"], float(row["started_at"]), None,
                                  row.get("identity"), row.get("returncode"))
            except (KeyError, TypeError, ValueError):
                continue
            if item.returncode is None and not self._alive(item.pid, item.identity):
                item.returncode = -1
            self.processes[item.name] = item
        self._write_state()

    def _running(self, item: DevProcess) -> bool:
        if item.proc is not None:
            code = item.proc.poll()
            if code is None:
                return True
            item.returncode = code
            return False
        if item.returncode is not None:
            return False
        if self._alive(item.pid, item.identity):
            return True
        item.returncode = -1
        return False

    def start(self, name: str, argv: list[str], cwd: str = ".") -> str:
        name = self._name(name)
        if name in self.processes and self._running(self.processes[name]):
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
                          str(log_path), time.time(), proc, self._identity(proc.pid))
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
            running = self._running(item)
            if running:
                state = "running/attached" if item.proc is not None else "running/reconciled"
            else:
                state = "exited(unknown)" if item.returncode == -1 else f"exited({item.returncode})"
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
        limit = max(1, min(12000, int(max_bytes)))
        with path.open('rb') as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - limit))
            clipped = f.read(limit)
        prefix = f"[... {size - len(clipped)} earlier bytes omitted ...]\n" if len(clipped) < size else ""
        return prefix + clipped.decode("utf-8", errors="replace")

    def wait(self, name: str, seconds: int = 1) -> str:
        name = self._name(name)
        item = self.processes.get(name)
        if item is None:
            return f'ERROR: unknown process {name!r}'
        deadline = time.monotonic() + max(0, min(10, seconds))
        while self._running(item) and time.monotonic() < deadline:
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        state = 'pending' if self._running(item) else (
            'exited; code unavailable after restart' if item.returncode == -1 else
            (f'completed exit={item.returncode}' if item.returncode == 0 else f'failed exit={item.returncode}'))
        self._write_state()
        return f'{name}: {state}\n{self.logs(name, 4000)}'

    def stop(self, name: str, grace_seconds: float = 2.0) -> str:
        name = self._name(name)
        item = self.processes.get(name)
        if not item:
            return f"{name}: unknown"
        if not self._running(item):
            return f"{name}: already exited"
        try:
            if os.name == "posix":
                os.killpg(item.pid, signal.SIGTERM)
            else:
                if item.proc is not None:
                    item.proc.terminate()
                else:
                    os.kill(item.pid, signal.SIGTERM)
            if item.proc is not None:
                item.proc.wait(timeout=grace_seconds)
            else:
                deadline = time.monotonic() + grace_seconds
                while self._alive(item.pid, item.identity) and time.monotonic() < deadline:
                    time.sleep(.05)
                if self._alive(item.pid, item.identity):
                    raise subprocess.TimeoutExpired(item.argv, grace_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(item.pid, signal.SIGKILL)
            else:
                if item.proc is not None:
                    item.proc.kill()
                else:
                    os.kill(item.pid, signal.SIGKILL)
            if item.proc is not None:
                item.proc.wait()
        self._write_state()
        item.returncode = item.proc.returncode if item.proc is not None else -1
        self._write_state()
        code = "unknown" if item.returncode == -1 else item.returncode
        return f"{name}: stopped exit={code}"

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
                "identity": item.identity,
                "returncode": item.proc.poll() if item.proc is not None else item.returncode,
            })
        atomic_json(self.state_path, {"version": 2, "processes": rows})
