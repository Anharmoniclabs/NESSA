"""Project-defined harness configuration: `agentharness.toml` at the project root.

The model selects among configured operations instead of rediscovering local commands:

    [context]
    max_chars = 12000                 # persistent-instruction budget

    [verification]
    continuous = ["syntax"]           # after every successful edit
    full_every_edits = 3              # run `tests` every N edits; 0 disables
    finish = ["syntax", "tests", "architecture"]

    [reviewer]
    every_edits = 2

    [checks]                          # shell commands, run in the private copy
    tests = "pnpm test"
    architecture = "pnpm architecture:check"

    [dev.app]                         # dev_start(name="app") needs no argv
    argv = ["pnpm", "dev"]
    cwd = "apps/web"
    health_url = "http://127.0.0.1:3000/health"

    [mcp.chrome-devtools]             # external tools through MCP
    transport = "stdio"
    argv = ["npx", "-y", "chrome-devtools-mcp@latest"]

    [telemetry]
    otlp_endpoint = "http://127.0.0.1:4318/v1/traces"

Invalid entries are reported and skipped; they never abort a run. Explicit command-line
options win over the file; the file wins over built-in defaults.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAMES = ("agentharness.toml", ".agent/agentharness.toml")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass
class DevPreset:
    argv: list[str]
    cwd: str = "."
    health_url: str = ""


@dataclass
class McpServer:
    name: str
    transport: str                       # stdio | http
    argv: list[str] = field(default_factory=list)
    url: str = ""
    env: dict = field(default_factory=dict)
    cwd: str = "."
    timeout: float = 30.0
    read_only: tuple[str, ...] = ()      # tool names treated as side-effect free
    allow_remote: bool = False


@dataclass
class ProjectConfig:
    path: str = ""
    context_max_chars: int | None = None
    continuous: tuple[str, ...] | None = None
    finish: tuple[str, ...] | None = None
    full_every_edits: int | None = None
    review_every_edits: int | None = None
    checks: dict[str, str] = field(default_factory=dict)
    dev: dict[str, DevPreset] = field(default_factory=dict)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    otlp_endpoint: str = ""
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.path:
            return "(no agentharness.toml)"
        rows = [f"config: {self.path}"]
        if self.checks:
            rows.append("checks: " + ", ".join(f"{k}={v!r}" for k, v in self.checks.items()))
        if self.finish is not None:
            rows.append("finish verification: " + ", ".join(self.finish))
        if self.continuous is not None:
            rows.append("continuous verification: " + ", ".join(self.continuous))
        if self.full_every_edits is not None:
            rows.append(f"full tests every {self.full_every_edits} edits")
        for name, d in self.dev.items():
            rows.append(f"dev {name}: {d.argv} cwd={d.cwd}" + (f" health={d.health_url}" if d.health_url else ""))
        for name, m in self.mcp.items():
            rows.append(f"mcp {name}: {m.transport} " + (" ".join(m.argv) if m.transport == "stdio" else m.url))
        if self.otlp_endpoint:
            rows.append("telemetry: " + self.otlp_endpoint)
        rows += [f"! {e}" for e in self.errors]
        return "\n".join(rows)


def _strings(value, where: str, errors: list) -> tuple[str, ...] | None:
    if isinstance(value, list) and all(isinstance(x, str) and x for x in value):
        return tuple(value)
    errors.append(f"{where} must be a list of non-empty strings")
    return None


def _int(value, where: str, errors: list, low: int = 0) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= low:
        return value
    errors.append(f"{where} must be an integer >= {low}")
    return None


LOCAL_URL = r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?"
SECTIONS = {"context", "verification", "reviewer", "checks", "dev", "mcp", "telemetry"}


def _parse_verification(data: dict, cfg: ProjectConfig) -> None:
    errors = cfg.errors
    context = data.get("context", {})
    if "max_chars" in context:
        cfg.context_max_chars = _int(context["max_chars"], "context.max_chars", errors, 1000)
    verification = data.get("verification", {})
    if "continuous" in verification:
        cfg.continuous = _strings(verification["continuous"], "verification.continuous", errors)
    if "finish" in verification:
        cfg.finish = _strings(verification["finish"], "verification.finish", errors)
    if "full_every_edits" in verification:
        cfg.full_every_edits = _int(verification["full_every_edits"], "verification.full_every_edits", errors)
    reviewer = data.get("reviewer", {})
    if "every_edits" in reviewer:
        cfg.review_every_edits = _int(reviewer["every_edits"], "reviewer.every_edits", errors, 1)


def _parse_checks(data: dict, cfg: ProjectConfig) -> None:
    for name, command in data.get("checks", {}).items():
        if name == "syntax":
            cfg.errors.append("checks.syntax is built in and cannot be replaced")
        elif not NAME.fullmatch(name) or not isinstance(command, str) or not command.strip():
            cfg.errors.append(f"checks.{name} must be a non-empty command string")
        else:
            cfg.checks[name] = command.strip()


def _parse_dev(data: dict, cfg: ProjectConfig) -> None:
    errors = cfg.errors
    for name, spec in data.get("dev", {}).items():
        where = f"dev.{name}"
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            errors.append(f"{where} must be a table with a valid name")
            continue
        argv = _strings(spec.get("argv"), f"{where}.argv", errors)
        health = spec.get("health_url", "")
        if health and not re.match(LOCAL_URL + r"(/|$)", str(health)):
            errors.append(f"{where}.health_url must be a local http(s) URL")
            health = ""
        if argv:
            cfg.dev[name] = DevPreset(list(argv), str(spec.get("cwd", ".")), str(health))


def _parse_mcp_server(name: str, spec: dict, errors: list) -> McpServer | None:
    """Build one validated server, or None (with the reason appended to errors)."""
    where = f"mcp.{name}"
    transport = spec.get("transport", "stdio")
    server = McpServer(name, transport, cwd=str(spec.get("cwd", ".")),
                       allow_remote=bool(spec.get("allow_remote", False)))
    if "timeout" in spec:
        if isinstance(spec["timeout"], (int, float)) and 1 <= spec["timeout"] <= 600:
            server.timeout = float(spec["timeout"])
        else:
            errors.append(f"{where}.timeout must be 1..600 seconds")
    if "read_only" in spec:
        server.read_only = _strings(spec["read_only"], f"{where}.read_only", errors) or ()
    env = spec.get("env", {})
    if isinstance(env, dict) and all(isinstance(v, str) for v in env.values()):
        server.env = dict(env)
    else:
        errors.append(f"{where}.env must map names to strings")
    if transport == "stdio":
        argv = _strings(spec.get("argv"), f"{where}.argv", errors)
        if not argv:
            return None
        server.argv = list(argv)
    elif transport == "http":
        url = str(spec.get("url", ""))
        if not url.startswith(("http://", "https://")):
            errors.append(f"{where}.url must be an http(s) URL")
            return None
        server.url = url
    else:
        errors.append(f"{where}.transport must be 'stdio' or 'http'")
        return None
    return server


def _parse_mcp(data: dict, cfg: ProjectConfig) -> None:
    for name, spec in data.get("mcp", {}).items():
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            cfg.errors.append(f"mcp.{name} must be a table with a valid name")
            continue
        server = _parse_mcp_server(name, spec, cfg.errors)
        if server is not None:
            cfg.mcp[name] = server


def _parse_telemetry(data: dict, cfg: ProjectConfig) -> None:
    endpoint = data.get("telemetry", {}).get("otlp_endpoint", "")
    if not endpoint:
        return
    if re.match(LOCAL_URL + "/", str(endpoint)):
        cfg.otlp_endpoint = str(endpoint)
    else:
        cfg.errors.append("telemetry.otlp_endpoint must be a local http(s) URL")


def load(repo: Path) -> ProjectConfig:
    repo = Path(repo)
    path = next((repo / n for n in FILE_NAMES if (repo / n).is_file()), None)
    cfg = ProjectConfig()
    if path is None:
        return cfg
    cfg.path = path.relative_to(repo).as_posix()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        cfg.errors.append(f"cannot parse {cfg.path}: {exc}")
        return cfg
    cfg.errors.extend(f"unknown section [{key}]" for key in data if key not in SECTIONS)
    for parse in (_parse_verification, _parse_checks, _parse_dev, _parse_mcp, _parse_telemetry):
        parse(data, cfg)
    return cfg


def apply_to_agent_config(config, project: ProjectConfig, explicit: set[str] = frozenset()):
    """Fill AgentConfig fields from the project file unless set explicitly by the caller."""
    pairs = {"verify": project.finish, "continuous_verify": project.continuous,
             "full_verify_every_edits": project.full_every_edits,
             "review_every_edits": project.review_every_edits}
    for name, value in pairs.items():
        if value is not None and name not in explicit:
            setattr(config, name, value)
    return config
