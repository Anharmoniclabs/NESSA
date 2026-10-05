"""Static HTML report for one run, built from its evidence directory.

Reads events.jsonl, result.json and timing.json; needs no server and no dependency.
Prompts and tool output are never read, so the report is safe to share. Every value
is HTML-escaped because event fields originate from the model and from tool output.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import telemetry

STYLE = ("body{font:14px system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem}"
         "table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}"
         "td,th{border-bottom:1px solid #ccc;padding:.25rem .5rem;text-align:left}"
         ".passed,.verified,.ok{color:#0a7d2c}.failed,.error,.timeout,.setup_error,.failed_checks{color:#b00020}")


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _cell(value) -> str:
    text = html.escape(str(value))
    return f'<span class="{text}">{text}</span>' if text.replace("_", "").isalpha() else text


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p>None recorded.</p>"
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def render(evidence_dir: Path) -> str:
    evidence_dir = Path(evidence_dir)
    events = telemetry._events(evidence_dir / "events.jsonl")
    if not events:
        raise FileNotFoundError(f"no events.jsonl in {evidence_dir}")
    result = _load(evidence_dir / "result.json") or {}
    timing = _load(evidence_dir / "timing.json") or {}
    receipt = result.get("receipt") or {}

    checks = [[e.get("phase", ""), e.get("name", ""), e.get("status", ""), f"{float(e.get('seconds') or 0):.1f}s"]
              for e in events if e.get("event") == "check"]
    edits = [[e.get("edit"), e.get("step"), ", ".join(e.get("changed") or [])]
             for e in events if e.get("event") == "checkpoint"]
    totals = [[kind, row["count"], f"{row['seconds']}s"] for kind, row in (timing.get("totals") or {}).items()]
    notable = [[e["seq"], e["event"], e.get("failing") or e.get("processes") or ""]
               for e in events if e.get("event") in ("stale_receipt", "repeated_failure", "dev_stop_failed",
                                                     "task_acceptance", "telemetry_error")]
    verdict = [["status", result.get("status", "unknown")], ["steps", result.get("steps", "")],
               ["changed files", ", ".join(result.get("changed_files") or [])],
               ["workspace sha", receipt.get("workspace_sha", "")[:16]],
               ["checks sha", receipt.get("checks_sha", "")[:16]]]
    sections = [("Result", _table(["field", "value"], verdict)),
                ("Checks", _table(["phase", "check", "status", "time"], checks)),
                ("Edit checkpoints", _table(["edit", "step", "files"], edits)),
                ("Time by kind", _table(["kind", "count", "seconds"], totals)),
                ("Integrity events", _table(["seq", "event", "detail"], notable))]
    body = "".join(f"<h2>{title}</h2>{content}" for title, content in sections)
    return (f"<!doctype html><meta charset=utf-8><title>Nessa run report</title><style>{STYLE}</style>"
            f"<h1>Nessa run report</h1><p>{html.escape(str(evidence_dir))}</p>{body}")
