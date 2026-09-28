"""Repository-local harness configuration.

Configuration is intentionally declarative. The model can choose among configured
operations, but it does not need to rediscover canonical checks and local dev commands
on every run.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DevProfile:
    name: str
    argv: tuple[str, ...]
    cwd: str = "."

    def describe(self) -> str:
        return f"{self.name}: cwd={self.cwd} argv={list(self.argv)!r}"


@dataclass
class ProjectConfig:
    path: Path | None = None
    checks: dict[str, str] = field(default_factory=dict)
    dev: dict[str, DevProfile] = field(default_factory=dict)
    continuous_verify: tuple[str, ...] = ()
    finish_verify: tuple[str, ...] = ()
    full_verify_every_edits: int | None = None
    review_every_edits: int | None = None
    context_max_chars: int | None = None

    def dev_summary(self) -> str:
        if not self.dev:
            return "(no configured dev profiles)"
        return "\n".join(self.dev[name].describe() for name in sorted(self.dev))


def _strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(x).strip() for x in value if str(x).strip())


def load_project_config(root: Path) -> ProjectConfig:
    root = Path(root).resolve()
    candidates = (root / "agentharness.toml", root / ".agent" / "config.toml")
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return ProjectConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    checks_raw = data.get("checks") or {}
    checks = {str(k): str(v) for k, v in checks_raw.items()
              if isinstance(k, str) and isinstance(v, str) and v.strip()}

    verify = data.get("verification") or {}
    context = data.get("context") or {}

    dev: dict[str, DevProfile] = {}
    for name, row in (data.get("dev") or {}).items():
        if not isinstance(row, dict):
            continue
        argv = _strings(row.get("argv"))
        if not argv:
            continue
        dev[str(name)] = DevProfile(str(name), argv, str(row.get("cwd") or "."))

    full_every = verify.get("full_every_edits")
    review_every = (data.get("reviewer") or {}).get("every_edits")
    max_chars = context.get("max_chars")

    return ProjectConfig(
        path=path,
        checks=checks,
        dev=dev,
        continuous_verify=_strings(verify.get("continuous")),
        finish_verify=_strings(verify.get("finish")),
        full_verify_every_edits=int(full_every) if isinstance(full_every, int) else None,
        review_every_edits=int(review_every) if isinstance(review_every, int) else None,
        context_max_chars=int(max_chars) if isinstance(max_chars, int) else None,
    )
