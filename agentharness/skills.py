"""Composable micro-harness skills.

A skill does not spawn another autonomous agent. It gives the current agent a focused
recipe, prerequisites, and verification contract, then returns control to the parent loop.

Repository skills live at .agent/skills/<name>/ with:
  SKILL.md       instructions
  skill.json     optional metadata:
                 {"description": "...", "tools": [...], "commands": [...], "checks": [...]}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    tools: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    source: str = "builtin"

    def render(self) -> str:
        req = []
        if self.tools:
            req.append("Harness tools: " + ", ".join(self.tools))
        if self.commands:
            req.append("Local commands/adapters: " + ", ".join(self.commands))
        if self.checks:
            req.append("Verification: " + ", ".join(self.checks))
        parts = [f"# Skill: {self.name}", self.description]
        if req:
            parts.append("\n".join(req))
        parts.append(self.instructions.strip())
        return "\n\n".join(p for p in parts if p)


BUILTINS = {
    "observe-local-app": Skill(
        "observe-local-app",
        "Launch and observe a local development application through managed dev-process tools.",
        """1. Inspect the repository manifests and instructions to find the canonical dev command.
2. Start the app with dev_start using argv (or a name configured in agentharness.toml [dev.NAME]),
   never by inventing a background shell pipeline.
3. Confirm it remains alive with dev_status, inspect dev_logs for startup errors, and probe
   dev_health when a local health URL exists.
4. Prefer the repository's existing telemetry configuration. If an OpenTelemetry collector or
   local dashboard is already defined, start it as another named dev process.
5. Reproduce the issue while collecting application logs and test evidence.
6. Stop temporary processes when they are no longer needed. Do not treat 'process is alive' as
   verification that the application behavior is correct.""",
        ("list_dir", "read_file", "dev_start", "dev_status", "dev_logs", "dev_health", "dev_stop", "run_check"),
        (),
        ("startup", "behavior", "tests"),
    ),
    "browser-debug": Skill(
        "browser-debug",
        "Attach browser debugging to the local application instead of reasoning from static HTML alone.",
        """1. Ensure the application is running with the local dev-process harness.
2. Prefer a configured Chrome DevTools MCP adapter when present: `[mcp.chrome-devtools]` in
   agentharness.toml exposes tools named mcp__chrome-devtools__*. The browser session
   must be a development/debug session, not a personal authenticated browsing profile.
3. Inspect console errors, network failures, DOM state and performance traces relevant to the task.
4. Correlate browser observations with source files and application logs.
5. After a fix, repeat the same browser observation and then run the repository's normal tests.
6. Save concise evidence (URL, observation, relevant error/trace, post-fix result) into the run log.""",
        ("dev_status", "dev_logs", "dev_health", "run_command", "run_check"),
        ("chrome", "node"),
        ("browser reproduction", "post-fix browser check", "tests"),
    ),
    "architecture-check": Skill(
        "architecture-check",
        "Check codebase structure and dependency contracts, not only syntax.",
        """1. Discover the repository's existing lint, workspace and architecture configuration.
2. Run existing structural rules before inventing new ones. Common adapters include ESLint custom
   rules/module boundaries, dependency-cruiser, ast-grep rules and repository-specific conformance tests.
3. Treat architectural violations as first-class verification failures when the project declares them.
4. When adding a new rule, make it narrow, deterministic and test it against one allowed and one
   forbidden example.
5. Do not rewrite broad sections of the codebase merely to satisfy a new rule without showing the diff.""",
        ("list_dir", "search", "read_file", "run_command", "run_check"),
        (),
        ("architecture", "lint", "tests"),
    ),
    "scaffold-project": Skill(
        "scaffold-project",
        "Create a new application or game as a complete project: package, modules, tests, README, launch.",
        """Build a complete, runnable project, not a single script. Use Python and the standard library
unless the user asks otherwise.
1. Layout: README.md (what it is, how to run, how to test); a package directory named after the
   project with focused modules (data models, core rules/engine, players or services, user interface,
   __main__.py entry point); tests/ with unittest tests for the core logic; requirements.txt only
   when third-party packages are truly needed; a one-line run command (python -m <package>).
2. Interactive programs and games get a GUI (tkinter); they cannot read terminal input when launched.
3. Work in small steps: one file per write_file call, each under about 150 lines. Write the core
   models and rules first, then their tests, and run_check tests until they pass.
4. Then write the interface and entry point, run_check syntax, and launch with dev_start
   (argv like ["python3", "-m", "<package>"]). Confirm with dev_status and dev_logs.
5. Finish with how to run and test it, and what is not implemented yet.""",
        ("list_dir", "write_file", "read_file", "run_check", "dev_start", "dev_status", "dev_logs"),
        (),
        ("tests",),
    ),
}

SCAFFOLD_RECIPE = BUILTINS["scaffold-project"].instructions


class SkillRegistry:
    def __init__(self, root: Path, *, include_builtins: bool = True):
        self.root = Path(root).resolve()
        self._skills: dict[str, Skill] = dict(BUILTINS) if include_builtins else {}
        self._discover()

    def _discover(self) -> None:
        base = self.root / ".agent" / "skills"
        if not base.is_dir():
            return
        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            instructions_path = directory / "SKILL.md"
            if not instructions_path.is_file():
                continue
            try:
                instructions = instructions_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = {}
            meta_path = directory / "skill.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
            name = str(meta.get("name") or directory.name)
            description = str(meta.get("description") or f"Repository skill {name}")
            self._skills[name] = Skill(
                name=name,
                description=description,
                instructions=instructions,
                tools=tuple(str(x) for x in meta.get("tools", [])),
                commands=tuple(str(x) for x in meta.get("commands", [])),
                checks=tuple(str(x) for x in meta.get("checks", [])),
                source=f"repo:{instructions_path.relative_to(self.root).as_posix()}",
            )

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"Unknown skill {name!r}; available: {', '.join(self.names())}") from exc

    def summary(self) -> str:
        return "\n".join(
            f"- {name}: {self._skills[name].description} [{self._skills[name].source}]"
            for name in self.names()
        ) or "(no skills)"
