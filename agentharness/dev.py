"""Managed local development processes for skills and debugging.

This is a developer-experience harness, not a security sandbox. Commands are argv arrays
(no shell expansion), logs are captured under the run evidence directory, and the parent
agent can start/status/tail/stop named processes without nesting another agent.

Process state is durable: `dev/processes.json` records each process with its kernel start
time. A new manager over the same evidence directory (a resumed session, the next desktop
turn, or a restart after a crash) reattaches to processes that are still alive, and only
when pid and start time both match, so a reused pid is never mistaken for ours.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_URL = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)")


def _start_ticks(pid: int) -> int | None:
    """Kernel start time of pid (Linux), or None when it no longer exists or is a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    if fields[0] == "Z":
        return None
    return int(fields[19])


@dataclass
class DevProcess:
    name: str
    argv: list[str]
    cwd: str
    pid: int
    log_path: str
    started_at: float
    proc: subprocess.Popen | None = None     # None when reattached after a restart
    start_ticks: int | None = None
    health_url: str = ""
    returncode: int | str | None = None       # "unknown" when a reattached process exited
    adopted: bool = field(default=False)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.proc is not None:
            self.returncode = self.proc.poll()
            return self.returncode
        try:  # our own child from an earlier manager: reap it to learn the exit code
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
                return self.returncode
        except ChildProcessError:
            pass
        if _start_ticks(self.pid) != self.start_ticks:
            self.returncode = "unknown"
        return self.returncode


class DevProcessManager:
    def __init__(self, workspace, evidence_dir: Path, presets: dict | None = None):
        self.workspace = workspace
        self.dir = Path(evidence_dir) / "dev"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.presets = dict(presets or {})
        self.processes: dict[str, DevProcess] = {}
        self.reattached = self._reconcile()

    @staticmethod
    def _name(name: str) -> str:
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("process name must match [A-Za-z0-9_.-]{1,64}")
        return name

    def _reconcile(self) -> list[str]:
        path = self.dir / "processes.json"
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        reattached = []
        for row in rows:
            try:
                item = DevProcess(row["name"], row["argv"], row["cwd"], int(row["pid"]), row["log_path"],
                                  row["started_at"], None, row.get("start_ticks"), row.get("health_url", ""),
                                  row.get("returncode"), True)
            except (KeyError, TypeError, ValueError):
                continue
            if item.returncode is None and (item.start_ticks is None or _start_ticks(item.pid) != item.start_ticks):
                item.returncode = "unknown"
            elif item.returncode is None:
                reattached.append(item.name)
            self.processes[item.name] = item
        self._write_state()
        return reattached

    def start(self, name: str, argv: list[str] | None = None, cwd: str | None = None) -> str:
        name = self._name(name)
        preset = self.presets.get(name)
        if argv is None:
            if preset is None:
                configured = ", ".join(sorted(self.presets)) or "none"
                raise ValueError(f"argv is required; configured dev processes: {configured}")
            argv, cwd = list(preset.argv), (cwd or preset.cwd)
        cwd = cwd or "."
        if name in self.processes and self.processes[name].poll() is None:
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
        item = DevProcess(name, list(argv), self.workspace.rel(workdir), proc.pid, str(log_path), time.time(),
                          proc, _start_ticks(proc.pid), preset.health_url if preset and argv == preset.argv else "")
        time.sleep(0.15)
        self.processes[name] = item
        self._write_state()
        if item.poll() is not None:
            return f"{name}: exited immediately with code {item.returncode}\n{self.logs(name)}"
        health = f" health={item.health_url}" if item.health_url else ""
        return f"{name}: running pid={proc.pid} cwd={item.cwd} log={log_path}{health}"

    def status(self, name: str | None = None) -> str:
        names = [self._name(name)] if name else sorted(self.processes)
        if not names:
            configured = f" Configured: {', '.join(sorted(self.presets))}" if self.presets else ""
            return "(no managed dev processes)" + configured
        rows = []
        for key in names:
            item = self.processes.get(key)
            if not item:
                rows.append(f"{key}: unknown" + (" (configured, not started)" if key in self.presets else ""))
                continue
            code = item.poll()
            state = "running" if code is None else f"exited({code})"
            note = " reattached" if item.adopted and code is None else ""
            rows.append(f"{key}: {state} pid={item.pid} cwd={item.cwd}{note}")
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
        while item.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        code = item.poll()
        state = 'pending' if code is None else (f'completed exit={code}' if code == 0 else f'failed exit={code}')
        self._write_state()
        return f'{name}: {state}\n{self.logs(name, 4000)}'

    def health(self, name: str, url: str = "", timeout: float = 3.0) -> str:
        """Probe a local HTTP endpoint. Process liveness alone is not application health."""
        name = self._name(name)
        item = self.processes.get(name)
        preset = self.presets.get(name)
        url = url or (item.health_url if item else "") or (preset.health_url if preset else "")
        if not url:
            return f"ERROR: no health URL for {name!r}; pass url or set dev.{name}.health_url"
        if not LOCAL_URL.match(url):
            return "ERROR: health probes are limited to local http(s) URLs"
        state = "not started" if item is None else ("running" if item.poll() is None else f"exited({item.returncode})")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
                body = response.read(600).decode("utf-8", "replace")
                return (f"{name}: healthy HTTP {response.status} in {time.monotonic() - started:.2f}s "
                        f"process={state} url={url}\n{body}")
        except urllib.error.HTTPError as exc:
            exc.close()
            return f"ERROR: {name}: unhealthy HTTP {exc.code} process={state} url={url}"
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return f"ERROR: {name}: unreachable ({reason}) process={state} url={url}"

    def stop(self, name: str, grace_seconds: float = 2.0) -> str:
        name = self._name(name)
        item = self.processes.get(name)
        if not item:
            return f"{name}: unknown"
        if item.poll() is not None:
            return f"{name}: already exited({item.returncode})"
        self._signal(item, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while item.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if item.poll() is None:
            self._signal(item, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while item.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        self._write_state()
        return f"{name}: stopped exit={item.poll()}"

    @staticmethod
    def _signal(item: DevProcess, sig) -> None:
        try:
            if os.name == "posix":
                os.killpg(item.pid, sig)
            elif item.proc is not None:
                item.proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

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
                "start_ticks": item.start_ticks,
                "log_path": item.log_path,
                "started_at": item.started_at,
                "health_url": item.health_url,
                "returncode": item.poll(),
            })
        tmp = self.dir / "processes.json.tmp"
        tmp.write_text(json.dumps(rows, indent=2))
        os.replace(tmp, self.dir / "processes.json")
