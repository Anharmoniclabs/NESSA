"""Native recurrent-model inference: capabilities, validated settings, backend adapters.

"Native recurrence" means the *backend* runs extra recurrent steps over hidden state inside
one inference call (it owns hidden states, recurrent layers and recurrence-aware caches).
It is not repeated chat requests and not agent refinement passes: one logical chat request
stays exactly one HTTP request. NESSA never patches ordinary transformer models.

Nothing is inferred from a model name or from generic OpenAI compatibility. A backend
supports recurrence only if the operator declares it explicitly (capability + adapter);
the default is unsupported. Invalid or unsupported requests raise `RecurrenceError` before
any network traffic, and settings are never silently dropped.

No concrete backend ships here: no recurrent checkpoint with authoritative inference
documentation has been verified, so only the generic `field-mapping` adapter exists, which
maps steps to field names that the operator copies from their backend's documentation.
"""
from __future__ import annotations

import statistics
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RESERVED_FIELDS = frozenset({"model", "messages", "temperature", "max_tokens", "tools", "tool_choice",
                             "stream", "stream_options", "reasoning_effort"})


class RecurrenceError(ValueError):
    """Unsupported or invalid recurrence request; raised before any inference."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_keys(data, allowed: set, where: str) -> None:
    if not isinstance(data, dict):
        raise RecurrenceError(f"{where} must be a table")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise RecurrenceError(f"unknown {where} setting(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class BackendCapability:
    """What the backend explicitly declares. The default is: no recurrence support."""
    native_recurrence: bool = False
    steps: tuple = ()                   # explicit supported set, or
    min_steps: int | None = None        # an inclusive range
    max_steps: int | None = None
    reports_actual_usage: bool = False  # backend returns the steps it really executed
    backend_id: str = ""

    def supports(self, steps: int) -> bool:
        if not self.native_recurrence:
            return False
        if self.steps:
            return steps in self.steps
        return self.min_steps is not None and self.min_steps <= steps <= self.max_steps

    def describe_supported(self) -> str:
        if self.steps:
            return "one of " + ", ".join(map(str, self.steps))
        return f"{self.min_steps}..{self.max_steps}"

    def to_dict(self) -> dict:
        out = dict(native_recurrence=self.native_recurrence, reports_actual_usage=self.reports_actual_usage,
                   backend_id=self.backend_id)
        if self.steps:
            out["steps"] = list(self.steps)
        if self.min_steps is not None:
            out.update(min_steps=self.min_steps, max_steps=self.max_steps)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "BackendCapability":
        _check_keys(data, {"native_recurrence", "steps", "min_steps", "max_steps",
                           "reports_actual_usage", "backend_id"}, "recurrence.capability")
        native = data.get("native_recurrence", False)
        reports = data.get("reports_actual_usage", False)
        if not isinstance(native, bool) or not isinstance(reports, bool):
            raise RecurrenceError("capability native_recurrence and reports_actual_usage must be booleans")
        backend_id = data.get("backend_id", "")
        if not isinstance(backend_id, str):
            raise RecurrenceError("capability backend_id must be a string")
        steps = data.get("steps", ())
        if not isinstance(steps, (list, tuple)) or not all(_is_int(s) and s >= 1 for s in steps):
            raise RecurrenceError("capability steps must be a list of integers >= 1")
        low, high = data.get("min_steps"), data.get("max_steps")
        if (low is None) != (high is None):
            raise RecurrenceError("capability min_steps and max_steps must be declared together")
        if low is not None and not (_is_int(low) and _is_int(high) and 1 <= low <= high):
            raise RecurrenceError("capability min_steps/max_steps must be integers with 1 <= min <= max")
        if native and bool(steps) == (low is not None):
            raise RecurrenceError("a recurrent backend must declare exactly one of steps or min_steps/max_steps")
        if not native and (steps or low is not None):
            raise RecurrenceError("capability declares supported steps but native_recurrence is false")
        return cls(native, tuple(sorted(set(steps))), low, high, reports, backend_id)


class RecurrenceAdapter:
    """Maps a validated step count to the transport fields a specific backend documents."""
    name = "adapter"
    can_report_usage = False   # True when reported_steps() can return the executed step count

    def request_fields(self, steps: int) -> dict:
        raise NotImplementedError

    def reported_steps(self, response: dict):
        """Actual steps executed according to the response, or None when not reported."""
        return None


def _nest(path: str, value) -> dict:
    out = current = {}
    parts = path.split(".")
    for part in parts[:-1]:
        current[part] = current = {}
    current[parts[-1]] = value
    return out


class FieldMappingAdapter(RecurrenceAdapter):
    """Operator-declared mapping: `request_field` (dotted path, e.g. "recurrence.steps") in the
    request body and optional `usage_field` (dotted path, e.g. "usage.recurrence_steps") in the
    response. Field names must be copied from the backend's own documentation."""
    name = "field-mapping"

    def __init__(self, request_field: str, usage_field: str = ""):
        for label, path in (("request_field", request_field), ("usage_field", usage_field)):
            if path and not all(p.isidentifier() for p in path.split(".")):
                raise RecurrenceError(f"adapter option {label} must be a dotted identifier path")
        if not request_field:
            raise RecurrenceError("adapter option request_field is required")
        if request_field.split(".")[0] in RESERVED_FIELDS:
            raise RecurrenceError(f"request_field {request_field!r} collides with a standard chat field")
        self.request_field, self.usage_field = request_field, usage_field
        self.can_report_usage = bool(usage_field)

    def request_fields(self, steps: int) -> dict:
        return _nest(self.request_field, steps)

    def reported_steps(self, response: dict):
        if not self.usage_field:
            return None
        value = response
        for part in self.usage_field.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value


def _field_mapping(options: dict) -> FieldMappingAdapter:
    _check_keys(options, {"request_field", "usage_field"}, "recurrence.options")
    for key, value in options.items():
        if not isinstance(value, str):
            raise RecurrenceError(f"adapter option {key} must be a string")
    return FieldMappingAdapter(options.get("request_field", ""), options.get("usage_field", ""))


ADAPTERS = {"field-mapping": _field_mapping}


def register_adapter(name: str, factory) -> None:
    """Extension point: `factory(options: dict) -> RecurrenceAdapter` for a documented backend."""
    ADAPTERS[name] = factory


@dataclass(frozen=True)
class RecurrenceConfig:
    enabled: bool = False
    steps: int | None = None
    adapter: str = ""
    options: dict = field(default_factory=dict)
    capability: BackendCapability = field(default_factory=BackendCapability)

    def build_adapter(self) -> RecurrenceAdapter:
        """Validate everything and return the adapter; raises RecurrenceError before inference."""
        if self.steps is not None and (not _is_int(self.steps) or self.steps < 1):
            raise RecurrenceError(f"recurrence steps must be an integer >= 1, got {self.steps!r}")
        if not self.enabled:
            return RecurrenceAdapter()
        if self.steps is None:
            raise RecurrenceError("recurrence is enabled but no steps were requested")
        cap = self.capability
        if not cap.native_recurrence:
            raise RecurrenceError("recurrence requested but the backend has no explicit native-recurrence "
                                  "capability declaration (default: unsupported)")
        if not cap.supports(self.steps):
            raise RecurrenceError(f"backend supports recurrence steps {cap.describe_supported()}; "
                                  f"requested {self.steps}")
        factory = ADAPTERS.get(self.adapter)
        if factory is None:
            raise RecurrenceError(f"unknown recurrence adapter {self.adapter!r}; "
                                  f"available: {', '.join(sorted(ADAPTERS)) or '(none)'}")
        adapter = factory(dict(self.options))
        if cap.reports_actual_usage and not adapter.can_report_usage:
            raise RecurrenceError("capability reports_actual_usage is true but the adapter cannot read it")
        if adapter.can_report_usage and not cap.reports_actual_usage:
            raise RecurrenceError("adapter reads actual usage but the capability does not declare "
                                  "reports_actual_usage")
        return adapter

    def to_dict(self) -> dict:
        return dict(enabled=self.enabled, steps=self.steps, adapter=self.adapter,
                    options=dict(self.options), capability=self.capability.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "RecurrenceConfig":
        _check_keys(data, {"enabled", "steps", "adapter", "options", "capability"}, "recurrence")
        enabled, steps = data.get("enabled", False), data.get("steps")
        if not isinstance(enabled, bool):
            raise RecurrenceError("recurrence.enabled must be a boolean")
        if steps is not None and (not _is_int(steps) or steps < 1):
            raise RecurrenceError(f"recurrence.steps must be an integer >= 1, got {steps!r}")
        adapter = data.get("adapter", "")
        if not isinstance(adapter, str):
            raise RecurrenceError("recurrence.adapter must be a string")
        options = data.get("options", {})
        if not isinstance(options, dict):
            raise RecurrenceError("recurrence.options must be a table")
        cap = BackendCapability.from_dict(data.get("capability", {}))
        return cls(enabled, steps, adapter, dict(options), cap)


def load_file(path) -> RecurrenceConfig:
    """Read the `[recurrence]` table of a TOML file; absent table means disabled."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RecurrenceError(f"cannot read recurrence config {path}: {exc}") from exc
    return RecurrenceConfig.from_dict(data.get("recurrence", {}))


def from_cli(steps, config_path) -> RecurrenceConfig | None:
    """Combine `--recurrence-steps` and `--recurrence-config`; None when nothing was requested."""
    if steps is None and not config_path:
        return None
    if steps is not None and not config_path:
        raise RecurrenceError("--recurrence-steps needs --recurrence-config with an explicit backend "
                              "capability and adapter declaration")
    cfg = load_file(config_path)
    if steps is not None:
        cfg = RecurrenceConfig(True, steps, cfg.adapter, cfg.options, cfg.capability)
    cfg.build_adapter()
    return cfg


def actual_steps(adapter: RecurrenceAdapter, capability: BackendCapability, response: dict):
    """Reported steps only when declared reportable and a non-negative integer; otherwise None."""
    if not capability.reports_actual_usage:
        return None
    value = adapter.reported_steps(response)
    return value if _is_int(value) and value >= 0 else None


def plan_arms(data: dict, config: RecurrenceConfig) -> list[dict]:
    """Evaluation arms comparing native depths under identical overall budgets.

    Agent passes (`agent_max_steps`) are budgeted separately and never trade against depth.
    """
    _check_keys(data, {"recurrence", "evaluation"}, "evaluation file")
    ev = data.get("evaluation", {})
    _check_keys(ev, {"depths", "include_baseline", "budget"}, "evaluation")
    depths = ev.get("depths", [])
    if not isinstance(depths, list) or not depths or not all(_is_int(d) and d >= 1 for d in depths):
        raise RecurrenceError("evaluation.depths must be a non-empty list of integers >= 1")
    budget = ev.get("budget", {})
    _check_keys(budget, {"agent_max_steps", "time_budget", "max_tokens"}, "evaluation.budget")
    budget = {"agent_max_steps": 40, "time_budget": 1800, "max_tokens": 4096, **budget}
    arms = [dict(name="baseline", recurrence_steps=None, **budget)] if ev.get("include_baseline", True) else []
    for depth in sorted(set(depths)):
        build_cfg = RecurrenceConfig(True, depth, config.adapter, config.options, config.capability)
        build_cfg.build_adapter()
        arms.append(dict(name=f"native-{depth}", recurrence_steps=depth, **budget))
    return arms


def summarize_arm(rows: list[dict]) -> dict:
    """rows: {status, truth: bool (independent ground truth), seconds, prompt_tokens?,
    recurrence_requested?, recurrence_actual? (None = unknown)}."""
    n = len(rows)
    claimed = [r for r in rows if r.get("status") == "verified"]
    done = [r for r in claimed if r.get("truth") is True]
    secs = [float(r["seconds"]) for r in rows if r.get("seconds") is not None]
    toks = [r["prompt_tokens"] for r in rows if r.get("prompt_tokens") is not None]
    req = [r["recurrence_requested"] for r in rows if r.get("recurrence_requested") is not None]
    act = [r["recurrence_actual"] for r in rows if r.get("recurrence_actual") is not None]
    false = [r for r in claimed if r.get("truth") is False]
    return dict(tasks=n, verified_completion=len(done), false_success=len(false),
                unlabelled_claims=len(claimed) - len(done) - len(false),
                latency_mean_s=round(statistics.fmean(secs), 3) if secs else None,
                latency_median_s=round(statistics.median(secs), 3) if secs else None,
                prompt_tokens=sum(toks) if toks else None,
                recurrence_requested_total=sum(req) if req else None,
                recurrence_actual_total=sum(act) if act else None,
                recurrence_actual_unknown=len(req) - len(act))
