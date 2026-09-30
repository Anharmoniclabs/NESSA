"""Read-only, loopback-only viewer for existing harness evidence (no model calls).

    python -m agentharness.viewer --runs ~/.agentharness/runs --port 8765
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_FILE = 2 * 1024 * 1024
MAX_EVENTS = 20000
MAX_RUNS = 500
MAX_DIRS = 4000
MAX_DEPTH = 6
POLL_SECONDS = 10
SKIP_DIRS = {"repo", "baseline", "patch-replay", "models", "home", "node_modules", "__pycache__", "venv", "portable"}
EVIDENCE_PATHS = ("work/evidence", "evidence", "")
MARKERS = {"result.json", "events.jsonl", "patch.diff"}
JOB_FILES = {"job.json", "job-result.json", "audit.json", "multifile-audit.json", "cancelled.json", "resource-summary.json"}
STATUSES = {"verified", "unverified", "improved", "failed_checks", "no_change", "stalled", "budget_exhausted", "rejected", "no_plan", "error", "blocked", "cancelled", "not_run"}
CHECK_STATUSES = {"passed", "failed", "timeout", "setup_error", "no_tests", "error", "not_run", "skipped"}
PRIVATE_MARKUP = re.compile(r"<\s*(?:think|thinking|reasoning|analysis)\b|\[start\]assistant\[channel\]analysis|<\|(?:analysis|channel)\|>", re.I)
SECRET = re.compile(
    r"(?im)(\b[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|access[_-]?token|auth[_-]?token|"
    r"password|passwd|secret|authorization|credential)[A-Za-z0-9_]*\b[\"']?\s*\]?\s*[:=]\s*)"
    r"([^\r\n]+)|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b")
SENSITIVE_PATH = re.compile(r"(?:^|/)(?:\.env(?:\.[^/]*)?|id_(?:rsa|ed25519)|credentials(?:\.[^/]*)?|secrets?(?:\.[^/]*)?)$", re.I)


def clean(value, limit=2000):
    """Do not expose private channel text; redact recognizable credential values."""
    if not isinstance(value, str):
        return None
    if PRIVATE_MARKUP.search(value):
        return "[Text omitted: private-reasoning markup detected]"
    if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
        return "[Text omitted: private key detected]"
    value = SECRET.sub(lambda m: (m.group(1) or "") + "[redacted]", value)
    value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", value)
    value = "".join(c for c in value if c in "\n\t" or ord(c) >= 32)
    return value if len(value) <= limit else value[:limit] + "\n[display truncated]"


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def strings(value, limit=60):
    return [clean(v, 1000) for v in value[:limit] if isinstance(v, str)] if isinstance(value, list) else None


def project_plan(value):
    if not isinstance(value, dict):
        return None
    return {"goal": clean(value.get("goal")), **{k: strings(value.get(k)) for k in ("steps", "files", "checks")}}


def project_check(value):
    if isinstance(value, str):
        match = re.search(r"\]\s+(\w+)", value)
        status = match.group(1) if match and match.group(1) in CHECK_STATUSES else "unknown"
        return {"status": status, "recorded": clean(value, 1500)}
    if not isinstance(value, dict):
        return {"status": "unknown"}
    return {"status": value.get("status") if isinstance(value.get("status"), str) and value["status"] in CHECK_STATUSES else "unknown",
            "exit_code": number(value.get("exit_code")), "seconds": number(value.get("seconds")),
            "counts": {clean(k, 100): number(v) for k, v in list(value.get("counts", {}).items())[:60] if number(v) is not None} if isinstance(value.get("counts"), dict) else {}}


def project_grade(value, depth=0):
    """Only diagnostic grade fields. Never return arbitrary audit/job dictionaries."""
    if not isinstance(value, dict) or depth > 4:
        return None
    result = {}
    for key in ("status", "error", "reason"):
        if isinstance(value.get(key), str):
            result[key] = clean(value[key], 1000)
    for key in ("cases", "assertions", "returncode", "check", "apply", "apply_check"):
        if key in value:
            result[key] = number(value[key])
    if isinstance(value.get("patch_nonempty"), bool):
        result["patch_nonempty"] = value["patch_nonempty"]
    if isinstance(value.get("panels"), dict):
        result["panels"] = {clean(k, 100): project_grade(v, depth + 1) for k, v in list(value["panels"].items())[:30]}
    if isinstance(value.get("grade"), dict):
        result["grade"] = project_grade(value["grade"], depth + 1)
    return result


class SafeRoot:
    """Pinned root descriptor; every child is opened without following symlinks.

    This deliberately requires POSIX descriptor-relative opens. Never substitute a
    resolve-then-open check, which is vulnerable to directory replacement races.
    """
    def __init__(self, root):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("Secure viewer reads currently require Linux or macOS (POSIX O_NOFOLLOW).")
        path = Path(os.path.abspath(os.path.expanduser(str(root))))
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        except OSError:
            os.close(fd)
            raise ValueError("Runs root must be an existing readable directory with no symlink components.") from None
        self.fd = fd
        self.label = path.name or "/"

    def close(self):
        os.close(self.fd)

    def directory(self, relative=""):
        parts = relative.split("/") if relative else []
        if any(not p or p in (".", "..") or "\\" in p or "\x00" in p for p in parts):
            raise ValueError("Invalid evidence path")
        fd = os.dup(self.fd)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except Exception:
            os.close(fd)
            raise

    def entries(self, relative=""):
        fd = self.directory(relative)
        try:
            with os.scandir(fd) as entries:
                return [(e.name, e.is_dir(follow_symlinks=False), e.is_file(follow_symlinks=False)) for _, e in zip(range(MAX_DIRS + 1), entries)]
        finally:
            os.close(fd)

    def read(self, relative, limit=MAX_FILE):
        parent, _, name = relative.rpartition("/")
        if not name or name in (".", "..") or "\\" in name or "\x00" in name:
            return None, "unsafe path", None
        directory = fd = None
        try:
            directory = self.directory(parent)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                return None, "not a private regular file", None
            if meta.st_size > limit:
                return None, "exceeds size limit", meta.st_mtime
            chunks, total = [], 0
            while total <= limit:
                part = os.read(fd, min(65536, limit + 1 - total))
                if not part:
                    break
                chunks.append(part)
                total += len(part)
            if total > limit:
                return None, "exceeds size limit", meta.st_mtime
            return b"".join(chunks).decode("utf-8"), None, meta.st_mtime
        except FileNotFoundError:
            return None, "missing", None
        except (OSError, ValueError, UnicodeError):
            return None, "unreadable or unsafe", None
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)


def joined(*parts):
    return "/".join(p for p in parts if p)


class RunStore:
    def __init__(self, root):
        self.root = SafeRoot(root)
        self.lock = threading.Lock()
        self.index = {}
        self.updated = 0.0
        self.truncated = False

    def close(self):
        self.root.close()

    def discover(self):
        """Bounded discovery of known run layouts, never a general file explorer."""
        found, todo, visited = {}, [("", 0)], 0
        limited = False
        while todo and visited < MAX_DIRS and len(found) < MAX_RUNS:
            rel, depth = todo.pop()
            visited += 1
            try:
                entries = self.root.entries(rel)
            except (OSError, ValueError):
                continue
            names = {n for n, _, regular in entries if regular}
            evidence = None
            for candidate in EVIDENCE_PATHS:
                try:
                    children = names if not candidate else {n for n, _, regular in self.root.entries(joined(rel, candidate)) if regular}
                except (OSError, ValueError):
                    continue
                if children & MARKERS:
                    evidence = candidate
                    break
            if evidence is not None or names & {"job.json", "job-result.json", "cancelled.json"}:
                rid = hashlib.sha256(rel.encode()).hexdigest()[:24]
                found[rid] = (rel, evidence)
                continue
            dirs = sorted(n for n, is_dir, _ in entries if is_dir and not n.startswith(".") and n not in SKIP_DIRS)
            if depth < MAX_DEPTH:
                todo.extend((joined(rel, n), depth + 1) for n in dirs)
            elif dirs:
                limited = True
            limited |= len(entries) > MAX_DIRS
        self.index = found
        self.truncated = limited or bool(todo)
        self.updated = time.monotonic()

    def read_run(self, rid, details=True):
        rel, evidence = self.index[rid]
        warnings, available, mtimes = [], [], []

        def read(name, ev=False, json_file=True):
            path = joined(rel, evidence if ev else "", name)
            if ev and evidence is None:
                return None
            raw, error, modified = self.root.read(path)
            if modified is not None:
                mtimes.append(modified)
            if error:
                if error != "missing":
                    warnings.append(f"{joined(evidence if ev else '', name)}: {error}")
                return None
            available.append(joined(evidence if ev else "", name))
            if not json_file:
                return raw
            try:
                obj = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(obj, dict):
                    raise ValueError()
                return obj
            except (ValueError, RecursionError):
                warnings.append(f"{name}: malformed JSON object (may be mid-write)")
                return None

        result = read("result.json", True)
        job = read("job.json") or {}
        process = read("job-result.json") or {}
        cancelled = read("cancelled.json") or {}
        audit = read("audit.json") or read("multifile-audit.json") or {}
        resource = read("resource-summary.json") if details else {}
        raw_events = read("events.jsonl", True, False)
        events = []
        if raw_events is not None:
            lines = raw_events.splitlines()
            if len(lines) > MAX_EVENTS:
                warnings.append("Event limit reached; latest bounded events shown")
            malformed = 0
            for line in lines[-MAX_EVENTS:]:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and isinstance(event.get("event"), str):
                        events.append(event)
                    else:
                        malformed += 1
                except (ValueError, RecursionError):
                    malformed += 1
            if malformed:
                warnings.append(f"{malformed} incomplete or malformed event lines ignored")
        start = next((e for e in events if e.get("event") == "start"), {})
        end = next((e for e in reversed(events) if e.get("event") == "end"), {})
        raw_status = (result or {}).get("status") or end.get("status")
        status = raw_status if isinstance(raw_status, str) and raw_status in STATUSES else "unavailable"
        status_source = "result.json" if result and result.get("status") else "end event" if end.get("status") else None
        if not status_source:
            status = "in_progress_or_interrupted" if events else "unavailable"
        if isinstance(cancelled.get("status"), str) and cancelled["status"] in {"cancelled", "not_run"}:
            status = cancelled["status"]
            status_source = "cancelled.json"
        independent = project_grade(audit.get("independent_grade") or audit.get("external_grade"))
        summary = {"id": rid, "name": clean(rel or self.root.label, 350), "status": status,
                   "status_source": status_source, "updated": max(mtimes) if mtimes else None,
                   "model": clean(job.get("model") or start.get("model"), 200),
                   "independent_status": (independent or {}).get("status"), "warnings": warnings}
        if not details:
            return summary
        stages, timeline, failures, checks = [], [], [], {"baseline": {}, "final": {}}
        plan, approved, observed_changed = None, None, None
        phase = None
        for event in events:
            kind = event.get("event")
            if kind == "stage" and isinstance(event.get("phase"), str) and event["phase"] in {"intake", "solve", "respond"}:
                phase = event["phase"]
                if phase not in stages:
                    stages.append(phase)
                timeline.append({"event": "stage", "phase": phase, "t": number(event.get("t"))})
            elif kind == "plan":
                plan, approved = project_plan(event.get("plan")), None
            elif kind == "approval" and isinstance(event.get("approved"), bool):
                approved = event["approved"]
                timeline.append({"event": "approval", "approved": approved, "t": number(event.get("t"))})
            elif kind == "checkpoint":
                observed_changed = strings(event.get("changed"))
                timeline.append({"event": "checkpoint", "edit": number(event.get("edit")), "changed": observed_changed, "t": number(event.get("t"))})
            elif kind == "check" and isinstance(event.get("name"), str):
                group = "baseline" if event.get("phase") == "baseline" else "final" if event.get("phase") == "verify" else None
                if group:
                    checks[group][clean(event["name"], 100)] = project_check(event)
            elif kind in {"error", "rejection", "budget"}:
                failures.append({"event": kind, "phase": clean(event.get("phase"), 80), "message": clean(event.get("error") or event.get("reason"), 1500)})
        result = result or {}
        recorded_checks = result.get("checks")
        if isinstance(recorded_checks, dict):
            for group in ("baseline", "final"):
                if isinstance(recorded_checks.get(group), dict):
                    for name, check in list(recorded_checks[group].items())[:60]:
                        # Structured check events provide exit code/counts; result text is shown too.
                        existing = checks[group].get(clean(name, 100), {})
                        checks[group][clean(name, 100)] = {**existing, **project_check(check)}
        if cancelled.get("reason"):
            failures.append({"event": "cancellation", "message": clean(cancelled.get("reason"))})
        if number(process.get("exit_code")) not in (None, 0):
            failures.append({"event": "process", "message": f"Process exited with code {process['exit_code']} (not a check result)"})
        response = result.get("response_stage")
        narration = None
        if isinstance(response, dict):
            narration = {"status": clean(response.get("status"), 100), "text": clean(response.get("advisory_text"), 1500), "error": clean(response.get("error")), "authoritative": False}
        patch = read("patch.diff", True, False)
        patch_source = "patch.diff" if patch is not None else None
        if patch is None and isinstance(result.get("patch"), str):
            patch, patch_source = result["patch"], "result.json"
        if patch is None and evidence is not None:
            try:
                checkpoints = sorted(n for n, _, regular in self.root.entries(joined(rel, evidence, "checkpoints"))
                                     if regular and re.fullmatch(r"edit-[0-9]{4,8}\.diff", n))
            except (OSError, ValueError):
                checkpoints = []
            if checkpoints:
                name = "checkpoints/" + checkpoints[-1]
                patch = read(name, True, False)
                patch_source = name + " (checkpoint, not final)" if patch is not None else None
        if patch is not None:
            # A patch may itself contain credentials; suppress sensitive file sections.
            sections = re.split(r"(?=^diff --git )", patch, flags=re.M)
            safe = []
            for section in sections:
                paths, unsafe_header = [], False
                for header in re.findall(r"^(?:\+\+\+|---) (.+)$", section, re.M):
                    # Git C-quotes names containing special characters. JSON handles
                    # common escapes; unusual/octal encodings are omitted conservatively.
                    if header.startswith('"'):
                        try:
                            header = json.loads(header)
                        except (ValueError, TypeError):
                            unsafe_header = True
                            continue
                    if not isinstance(header, str):
                        unsafe_header = True
                    elif header.startswith(("a/", "b/")):
                        paths.append(header[2:].split("\t", 1)[0])
                if unsafe_header or any(SENSITIVE_PATH.search(path) for path in paths):
                    safe.append("[Patch section omitted: sensitive or unrecognized filename]\n")
                else:
                    safe.append(section)
            patch = clean("".join(safe), 100000)
        changed = strings(result.get("changed_files"))
        changed_source = "result.json" if changed is not None else None
        if changed is None:
            changed = strings(end.get("changed"))
            changed_source = "end event" if changed is not None else None
        if changed is None and observed_changed is not None:
            changed, changed_source = observed_changed, "latest checkpoint (not final)"
        metrics = {"run_seconds": number(result.get("seconds")), "active_seconds": number(result.get("active_seconds")),
                   "approval_seconds": number(result.get("approval_seconds")), "process_wall_seconds": number(process.get("wall_seconds")),
                   "steps": number(result.get("steps")), "prompt_tokens": number(audit.get("prompt_tokens")),
                   "completion_tokens": number(audit.get("completion_tokens")), "http_calls": number(audit.get("http_calls")),
                   "peak_runtime_rss_kib": number((resource or {}).get("peak_runtime_rss_kib"))}
        integrity = {key: audit.get(key) if isinstance(audit.get(key), bool) else None for key in
                     ("original_unchanged", "tests_unchanged", "original_matches_frozen_manifest", "change_scope_ok", "fully_verified")}
        return {**summary, "task": clean(job.get("task") or start.get("task"), 4000),
                "mode": clean(result.get("mode") or start.get("mode"), 100), "solve_mode": clean(result.get("solve_mode"), 100),
                "plan": project_plan(result.get("plan")) or plan, "approved": approved,
                "stages": stages, "last_stage": phase, "ended": bool(end or result.get("status")),
                "timeline": timeline[-80:], "changed_files": changed, "changed_source": changed_source,
                "checks": checks, "failures": failures[-30:], "metrics": metrics, "process_exit_code": number(process.get("exit_code")),
                "independent": independent, "replay": project_grade(audit.get("patch_replay")),
                "replay_tests": project_grade(audit.get("patch_replay_tests")),
                "independent_replay": project_grade(audit.get("independent_replay_grade")), "integrity": integrity,
                "advisory": narration, "patch": patch, "patch_source": patch_source,
                "evidence_files": available, "warnings": warnings}

    def listing(self):
        with self.lock:
            if time.monotonic() - self.updated >= 2:
                self.discover()
            runs = [self.read_run(rid, False) for rid in self.index]
            runs.sort(key=lambda r: (r["updated"] or 0, r["name"] or ""), reverse=True)
            return {"runs": runs, "root": clean(self.root.label, 200), "truncated": self.truncated,
                    "poll_seconds": POLL_SECONDS, "observed_at": time.time()}

    def detail(self, rid):
        with self.lock:
            if rid not in self.index:
                self.discover()
            if rid not in self.index:
                raise KeyError(rid)
            return self.read_run(rid)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, port=8765):
        self.store = store
        self.assets = {"/": ("text/html; charset=utf-8", "index.html"), "/app.js": ("text/javascript; charset=utf-8", "app.js"), "/style.css": ("text/css; charset=utf-8", "style.css")}
        self.assets = {route: (mime, (Path(__file__).with_name("viewer_static") / name).read_bytes()) for route, (mime, name) in self.assets.items()}
        super().__init__(("127.0.0.1", port), ViewerHandler)


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "NESSAViewer/1"
    sys_version = ""

    def log_message(self, *_args):
        pass  # Do not put paths, query strings or untrusted evidence into console logs.

    def respond(self, status, body, mime="application/json; charset=utf-8", head=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self, head=False):
        authority = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != authority or self.headers.get("Origin") not in (None, f"http://{authority}"):
            return self.respond(403, {"error": "Only this loopback origin is allowed"}, head=head)
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return self.respond(403, {"error": "Cross-site reads are not allowed"}, head=head)
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self.respond(400, {"error": "Invalid request path"}, head=head)
        if parsed.query or parsed.fragment or parsed.netloc or "%" in parsed.path or ".." in parsed.path or "\\" in parsed.path:
            return self.respond(400, {"error": "Unsupported request path"}, head=head)
        try:
            if parsed.path in self.server.assets:
                mime, body = self.server.assets[parsed.path]
                return self.respond(200, body, mime, head)
            if parsed.path == "/api/runs":
                return self.respond(200, self.server.store.listing(), head=head)
            match = re.fullmatch(r"/api/runs/([0-9a-f]{24})", parsed.path)
            if match:
                return self.respond(200, self.server.store.detail(match.group(1)), head=head)
            return self.respond(404, {"error": "Not found"}, head=head)
        except KeyError:
            return self.respond(404, {"error": "Run not found; refresh the list"}, head=head)
        except (OSError, ValueError, TypeError, RecursionError):
            return self.respond(503, {"error": "Evidence is temporarily unavailable; retry after refresh"}, head=head)

    def do_HEAD(self):
        self.do_GET(head=True)

    def method_not_allowed(self):
        self.respond(405, {"error": "This viewer is read-only"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = method_not_allowed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="Existing runs root; never reads parent directories")
    parser.add_argument("--port", type=int, default=8765, help="Loopback port (0 selects a free port)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        store = RunStore(args.runs)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        server = ViewerServer(store, args.port)
        print(f"NESSA read-only run viewer: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Existing evidence only. No model calls, edits, approvals, or patch application. Ctrl-C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except OSError as exc:
        parser.error(f"Cannot start loopback viewer: {exc}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
