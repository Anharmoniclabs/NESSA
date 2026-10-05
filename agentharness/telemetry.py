"""Run observability derived from the durable event log (no extra dependency).

At the end of a run, events.jsonl is converted into:
    timing.json   model/tool/check time totals, the slowest operations
    trace.json    OpenTelemetry OTLP/JSON spans: run -> model calls, tool operations, checks
When agentharness.toml sets `[telemetry] otlp_endpoint`, the same spans are POSTed to that
local collector. Export failures are recorded, never raised. Prompts and tool output are
not exported; spans carry names, statuses and timings only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def _events(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def spans_from_events(events: list[dict]) -> list[dict]:
    """Return flat spans: {name, kind, start, end, attributes, status}. Times are epoch seconds."""
    spans, started = [], {}
    for e in events:
        name, t = e.get("event"), float(e.get("t", 0))
        if name == "model":
            attrs = {"tool_calls": len(e.get("calls") or []), "prompt_tokens": int(e.get("prompt_tokens") or 0)}
            r = e.get("recurrence")
            if isinstance(r, dict):
                # Native backend recurrence only; unknown actual steps are omitted, never guessed.
                attrs["recurrence_requested_steps"] = int(r.get("requested_steps") or 0)
                if isinstance(r.get("actual_steps"), int):
                    attrs["recurrence_actual_steps"] = r["actual_steps"]
                attrs["recurrence_backend"] = r.get("backend") or ""
                attrs["recurrence_model"] = r.get("model") or ""
                attrs["recurrence_latency_s"] = float(r.get("latency_s") or 0)
            spans.append(dict(name="model", kind="model", start=t - float(e.get("seconds") or 0), end=t,
                              attributes=attrs, status="ok"))
        elif name == "operation_started":
            started[e.get("operation_id")] = (t, e.get("name", "tool"))
        elif name == "operation_finished" and e.get("operation_id") in started:
            begin, tool = started.pop(e["operation_id"])
            spans.append(dict(name=f"tool {tool}", kind="tool", start=begin, end=t,
                              attributes={"tool": tool, "result": e.get("status", "")},
                              status="error" if e.get("status") != "completed" else "ok"))
        elif name == "check":
            spans.append(dict(name=f"check {e.get('name')}", kind="check", start=t - float(e.get("seconds") or 0),
                              end=t, attributes={"check": e.get("name", ""), "phase": e.get("phase", ""),
                                                 "result": e.get("status", "")},
                              status="ok" if e.get("status") in ("passed", "no_tests") else "error"))
    return spans


def summarize(spans: list[dict], status: str, seconds: float) -> dict:
    totals: dict[str, dict] = {}
    for s in spans:
        row = totals.setdefault(s["kind"], {"count": 0, "seconds": 0.0})
        row["count"] += 1
        row["seconds"] = round(row["seconds"] + s["end"] - s["start"], 3)
    slowest = sorted(spans, key=lambda s: s["start"] - s["end"])[:5]
    models = [s for s in spans if s["kind"] == "model"]
    native = [s["attributes"] for s in models if "recurrence_requested_steps" in s["attributes"]]
    out = dict(status=status, wall_seconds=seconds, totals=totals,
               agent_passes=len(models),   # controller model turns: reported separately from native recurrence
               slowest=[{"name": s["name"], "seconds": round(s["end"] - s["start"], 3)} for s in slowest])
    if native:
        out["native_recurrence"] = dict(
            model_calls=len(native),
            requested_steps_total=sum(a["recurrence_requested_steps"] for a in native),
            actual_steps_total=(sum(a["recurrence_actual_steps"] for a in native if "recurrence_actual_steps" in a)
                                if any("recurrence_actual_steps" in a for a in native) else None),
            actual_unknown_calls=sum("recurrence_actual_steps" not in a for a in native),
            latency_s=round(sum(a["recurrence_latency_s"] for a in native), 3))
    return out


def otlp(spans: list[dict], run_name: str, status: str, start: float, end: float) -> dict:
    trace_id = os.urandom(16).hex()
    root_id = os.urandom(8).hex()
    nanos = lambda t: str(int(t * 1e9))
    code = lambda s: 1 if s == "ok" else 2   # STATUS_CODE_OK / STATUS_CODE_ERROR
    rows = [{"traceId": trace_id, "spanId": root_id, "name": run_name, "kind": 1,
             "startTimeUnixNano": nanos(start), "endTimeUnixNano": nanos(end),
             "attributes": [_attr("nessa.status", status)],
             "status": {"code": 1 if status in ("verified", "unverified", "answered", "no_change") else 2}}]
    for s in spans:
        rows.append({"traceId": trace_id, "spanId": os.urandom(8).hex(), "parentSpanId": root_id,
                     "name": s["name"], "kind": 3 if s["kind"] == "model" else 1,
                     "startTimeUnixNano": nanos(s["start"]), "endTimeUnixNano": nanos(s["end"]),
                     "attributes": [_attr("nessa.kind", s["kind"])] +
                                   [_attr("nessa." + k, v) for k, v in s["attributes"].items()],
                     "status": {"code": code(s["status"])}})
    return {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "nessa-agentharness")]},
                               "scopeSpans": [{"scope": {"name": "agentharness"}, "spans": rows}]}]}


def export_run(evidence_dir: Path, status: str, seconds: float, endpoint: str = "") -> dict:
    evidence_dir = Path(evidence_dir)
    events = _events(evidence_dir / "events.jsonl")
    spans = spans_from_events(events)
    # A resumed session appends to events.jsonl: trace only the latest run.
    starts = [e for e in events if e.get("event") in ("start", "resume")]
    begin = float(starts[-1]["t"]) if starts else (min((s["start"] for s in spans), default=0.0))
    spans = [s for s in spans if s["end"] >= begin]
    end = max([begin] + [s["end"] for s in spans] + [float(events[-1]["t"]) if events else begin])
    summary = summarize(spans, status, seconds)
    trace = otlp(spans, "nessa run", status, begin, end)
    (evidence_dir / "trace.json").write_text(json.dumps(trace, indent=1))
    if endpoint:
        try:
            request = urllib.request.Request(endpoint, json.dumps(trace).encode(),
                                             {"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                summary["export"] = f"HTTP {response.status} {endpoint}"
        except (urllib.error.URLError, OSError) as exc:
            summary["export"] = f"failed: {getattr(exc, 'reason', exc)} {endpoint}"
    (evidence_dir / "timing.json").write_text(json.dumps(summary, indent=2))
    return summary
