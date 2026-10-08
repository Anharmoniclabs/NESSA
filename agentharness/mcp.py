"""Model Context Protocol client adapters: external tools inside the one parent loop.

Servers come from `[mcp.NAME]` in agentharness.toml. Each discovered tool is registered as
`mcp__<server>__<tool>` with its JSON schema, a kind and bounded output. Tools are `read`
only when the server annotates them readOnlyHint or the config lists them in `read_only`;
everything else has kind `mcp` and is offered only after plan approval.

A failed or disconnected server produces an ERROR observation for the model and is
reconnected on the next call; it never raises into the controller or alters its state.
Transports: stdio (newline-delimited JSON-RPC) and streamable HTTP (JSON or SSE replies).
"""
from __future__ import annotations

import atexit
import base64
import uuid
import itertools
import json
import os
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .config import McpServer

PROTOCOL_VERSION = "2025-06-18"
OUTPUT_LIMIT = 12000
LOCAL = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)")


class McpError(Exception):
    pass


class _Transport:
    def request(self, method: str, params: dict | None, timeout: float) -> dict: ...
    def notify(self, method: str, params: dict | None = None) -> None: ...
    def close(self) -> None: ...


class StdioTransport(_Transport):
    def __init__(self, server: McpServer, cwd: Path, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr = (log_dir / f"mcp-{server.name}.log").open("ab")
        try:
            self.proc = subprocess.Popen(server.argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=self._stderr, env={**os.environ, **server.env},
                                         start_new_session=(os.name == "posix"))
        except OSError as exc:
            self._stderr.close()
            raise McpError(f"cannot start {server.argv[0]!r}: {exc}") from exc
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _send(self, message: dict) -> None:
        data = (json.dumps(message) + "\n").encode()
        with self._lock:
            if self.proc.poll() is not None:
                raise McpError(f"server exited with code {self.proc.returncode}")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError(f"server disconnected: {exc}") from exc

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:  # a request from the server
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "not supported by this client"}
                try:
                    self._send(reply)
                except McpError:
                    break
            elif "id" in message and message["id"] in self._waiting:
                self._waiting[message["id"]].put(message)
        for waiting in list(self._waiting.values()):
            waiting.put(None)

    def request(self, method, params, timeout):
        ident = next(self._ids)
        box = self._waiting[ident] = queue.Queue(maxsize=1)
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, **({"params": params} if params else {})})
            try:
                message = box.get(timeout=timeout)
            except queue.Empty:
                raise McpError(f"{method} timed out after {timeout:.0f}s") from None
        finally:
            self._waiting.pop(ident, None)
        if message is None:
            raise McpError(f"server disconnected (exit={self.proc.poll()})")
        return message

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reader.join(timeout=2)
        self.proc.stdout.close()
        self._stderr.close()


class HttpTransport(_Transport):
    def __init__(self, server: McpServer):
        if not LOCAL.match(server.url) and not server.allow_remote:
            raise McpError("remote MCP URL refused; set allow_remote = true to permit it")
        self.url = server.url
        self.session = ""
        self.version = ""
        self._ids = itertools.count(1)

    def _post(self, message: dict, timeout: float):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        request = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise McpError(f"HTTP {exc.code} from MCP server") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"server unreachable: {getattr(exc, 'reason', exc)}") from exc
        with response:
            self.session = response.headers.get("Mcp-Session-Id", self.session)
            body = response.read(8_000_000).decode("utf-8", "replace")
            kind = response.headers.get("Content-Type", "")
        if "text/event-stream" in kind:
            messages = []
            for event in re.split(r"\r?\n\r?\n", body):
                data = "\n".join(l[5:].lstrip() for l in event.splitlines() if l.startswith("data:"))
                if data:
                    try:
                        messages.append(json.loads(data))
                    except ValueError:
                        pass
            return messages
        return [json.loads(body)] if body.strip() else []

    def request(self, method, params, timeout):
        ident = next(self._ids)
        message = {"jsonrpc": "2.0", "id": ident, "method": method, **({"params": params} if params else {})}
        for reply in self._post(message, timeout):
            if isinstance(reply, dict) and reply.get("id") == ident:
                return reply
        raise McpError(f"no response to {method}")

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 10)

    def close(self):
        pass


class McpConnection:
    def __init__(self, server: McpServer, cwd: Path, log_dir: Path):
        self.server = server
        self.asset_dir = Path(log_dir) / "assets"
        self.transport = (StdioTransport(server, cwd, log_dir) if server.transport == "stdio"
                          else HttpTransport(server))
        try:
            reply = self._result("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                "clientInfo": {"name": "nessa", "version": "1"}})
            if isinstance(self.transport, HttpTransport):
                self.transport.version = reply.get("protocolVersion", PROTOCOL_VERSION)
            self.server_info = reply.get("serverInfo", {})
            self.transport.notify("notifications/initialized")
        except Exception:
            self.transport.close()
            raise

    def _result(self, method, params=None) -> dict:
        reply = self.transport.request(method, params, self.server.timeout)
        if "error" in reply:
            error = reply["error"] or {}
            raise McpError(f"{method}: {error.get('message', error)}")
        return reply.get("result") or {}

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            page = self._result("tools/list", {"cursor": cursor} if cursor else None)
            tools += [t for t in page.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, name: str, arguments: dict) -> str:
        result = self._result("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for item in result.get("content", []):
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            elif kind in ("image", "audio"):
                mime, encoded = item.get('mimeType', ''), item.get('data', '')
                extensions = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp',
                              'audio/wav': '.wav', 'audio/mpeg': '.mp3'}
                if mime in extensions and isinstance(encoded, str) and len(encoded) <= 22_000_000:
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                        self.asset_dir.mkdir(parents=True, exist_ok=True)
                        path = self.asset_dir / (uuid.uuid4().hex + extensions[mime])
                        path.write_bytes(raw)
                        parts.append(f'[{kind} artifact saved: {path}; visual/audio review not performed]')
                    except (ValueError, OSError):
                        parts.append(f'[{kind} artifact could not be saved]')
                else:
                    parts.append(f'[{kind} {mime}: unsupported or oversized artifact omitted]')
            elif kind == "resource":
                resource = item.get("resource", {})
                parts.append(resource.get("text") or f"[resource {resource.get('uri', '')}]")
            elif kind == "resource_link":
                parts.append(f"[resource {item.get('uri', '')}]")
        if not parts and "structuredContent" in result:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts) or "(no content)"
        return ("ERROR: " + text) if result.get("isError") else text

    def close(self):
        self.transport.close()


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


class McpBus:
    """Connections and discovered tools for one project's configured servers."""

    def __init__(self, servers: dict[str, McpServer], cwd: Path, log_dir: Path):
        self.servers, self.cwd, self.log_dir = dict(servers), Path(cwd), Path(log_dir)
        self.connections: dict[str, McpConnection] = {}
        self.discovered: dict[str, tuple[str, dict]] = {}   # harness name -> (server, tool spec)
        self.errors: dict[str, str] = {}
        self.lock = threading.Lock()
        for name in self.servers:
            try:
                for spec in self._connect(name).list_tools():
                    self.discovered.setdefault(tool_name(name, spec["name"]), (name, spec))
            except Exception as exc:
                self._drop(name)
                self.errors[name] = f"{type(exc).__name__}: {exc}"

    def _connect(self, name: str) -> McpConnection:
        if name not in self.connections:
            self.connections[name] = McpConnection(self.servers[name], self.cwd, self.log_dir)
        return self.connections[name]

    def _drop(self, name: str) -> None:
        connection = self.connections.pop(name, None)
        if connection:
            connection.close()

    def call(self, harness_name: str, arguments: dict) -> str:
        server, spec = self.discovered[harness_name]
        with self.lock:
            # A failed call may still have acted, so it is reported, never retried here.
            # The dropped connection is re-established on the next call.
            try:
                out = self._connect(server).call(spec["name"], arguments)
            except Exception as exc:
                self._drop(server)
                return f"ERROR: MCP server {server!r}: {type(exc).__name__}: {exc}"
        if len(out) > OUTPUT_LIMIT:
            out = out[:OUTPUT_LIMIT] + f"\n[... {len(out) - OUTPUT_LIMIT} chars omitted ...]"
        return out

    def tools(self) -> dict:
        from .tools import Tool
        result = {}
        for harness_name, (server, spec) in self.discovered.items():
            schema = spec.get("inputSchema") or {}
            annotations = spec.get("annotations") or {}
            read_only = annotations.get("readOnlyHint") is True or spec["name"] in self.servers[server].read_only
            description = f"[MCP {server}] " + str(spec.get("description") or spec["name"])[:600]
            result[harness_name] = Tool(
                harness_name, description, dict(schema.get("properties") or {}),
                tuple(schema.get("required") or ()), "read" if read_only else "mcp",
                handler=lambda c, a, n=harness_name: self.call(n, a), cacheable=False)
        return result

    def summary(self) -> str:
        rows = [f"{name}: {len([1 for s, _ in self.discovered.values() if s == name])} tools"
                + (f" (unavailable: {self.errors[name]})" if name in self.errors else "")
                for name in self.servers]
        return "\n".join(rows) or "(no MCP servers)"

    def close(self) -> None:
        for name in list(self.connections):
            self._drop(name)


_BUSES: dict[str, McpBus] = {}
_BUSES_LOCK = threading.Lock()


def shared_bus(servers: dict[str, McpServer], cwd: Path, log_dir: Path) -> McpBus:
    """Reuse discovered tools and live connections across turns of the same project."""
    key = json.dumps([str(Path(cwd).resolve()), {n: vars(s) for n, s in sorted(servers.items())}],
                     default=str, sort_keys=True)
    with _BUSES_LOCK:
        bus = _BUSES.get(key)
        if bus is None or bus.errors:
            if bus is not None:
                bus.close()
            bus = _BUSES[key] = McpBus(servers, cwd, log_dir)
        return bus


@atexit.register
def close_all() -> None:
    with _BUSES_LOCK:
        for bus in _BUSES.values():
            bus.close()
        _BUSES.clear()
