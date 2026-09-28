"""Tool registry. Each tool: JSON schema for the model + handler + a kind the controller uses.

kinds:  read (no side effects) | edit (changes the snapshot) | check | control
"""
from __future__ import annotations

import glob as globlib
import os
from dataclasses import dataclass
from typing import Callable

from . import extract as ex
from .checks import run_command
from .workspace import ToolError


@dataclass
class Tool:
    name: str
    description: str
    params: dict           # JSON-schema "properties"
    required: tuple = ()
    kind: str = "read"
    handler: Callable | None = None  # handler(ctx, args) -> str

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": list(self.required)}}}

    def validate(self, args) -> dict:
        if not isinstance(args, dict):
            raise ToolError("Arguments must be a JSON object.")
        missing = [k for k in self.required if k not in args]
        if missing:
            raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
        unknown = [k for k in args if k not in self.params]
        if unknown:
            raise ToolError(f"Unknown argument(s): {', '.join(unknown)}; allowed: {', '.join(self.params)}")
        for k, v in args.items():
            t = self.params[k].get("type")
            ok = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}.get(t)
            if ok is int and isinstance(v, str) and v.strip().lstrip("-").isdigit():
                args[k] = int(v)  # small models often quote numbers
            elif ok and not isinstance(v, ok):
                raise ToolError(f"Argument {k!r} must be {t}.")
        return args


S = {"type": "string"}
I = {"type": "integer"}


def _extract_paths(ctx, args) -> list:
    out = []
    for pattern in args["paths"]:
        for m in sorted(globlib.glob(str(ctx.ws.repo / pattern), recursive=True)):
            p = ctx.ws.path(os.path.relpath(m, ctx.ws.repo))  # raises if outside the project
            if p.is_file():
                out.append(p)
    if not out:
        raise ToolError("No files matched.")
    return out


def _extract_table(ctx, args) -> str:
    fields = [ex.Field(**f) for f in args.get("fields", [])]
    result = ex.extract(_extract_paths(ctx, args), fields, tables=args.get("tables", False),
                        kv=args.get("key_values", False))
    rows = result.records + result.tables
    if args.get("out"):
        dest = ctx.ws.path(args["out"])
        ex.write_rows(rows, dest)
        note = f"wrote {len(rows)} rows to {ctx.ws.rel(dest)}\n"
    else:
        note = ""
    return note + ex.to_markdown(rows[:40]) + (f"\n... {len(rows) - 40} more rows" if len(rows) > 40 else "")


def build_tools(allow_shell: bool = True, allow_extract: bool = True) -> dict[str, Tool]:
    tools = [
        Tool("list_dir", "List files in a project directory.",
             {"path": S, "depth": {**I, "description": "1-3, default 1"}},
             handler=lambda c, a: c.ws.list_dir(a.get("path", "."), min(3, int(a.get("depth", 1))))),
        Tool("search", "Regex search across project files. Returns path:line: text.",
             {"pattern": S, "path": S, "glob": {**S, "description": "e.g. *.py"},
              "ignore_case": {"type": "boolean"}}, ("pattern",),
             handler=lambda c, a: c.ws.search(a["pattern"], a.get("path", "."), a.get("glob"),
                                               ignore_case=a.get("ignore_case", False))),
        Tool("outline", "List classes/functions in a file with their line ranges.",
             {"path": S}, ("path",), handler=lambda c, a: c.ws.outline(a["path"])),
        Tool("read_file", "Read a page of a file with line numbers (default 200 lines).",
             {"path": S, "start": I, "end": I}, ("path",),
             handler=lambda c, a: c.ws.read(a["path"], a.get("start", 1), a.get("end"))),
        Tool("replace_in_file", "Replace one exact, unique snippet. Copy `old` exactly from read_file "
             "output (without the line numbers). Read the file first.",
             {"path": S, "old": S, "new": S}, ("path", "old", "new"), "edit",
             handler=lambda c, a: c.ws.replace(a["path"], a["old"], a["new"])),
        Tool("edit_lines", "Replace lines start..end (inclusive, 1-based) with `new`. Use end=start-1 "
             "to insert before `start`. Read the file first; line numbers shift after edits.",
             {"path": S, "start": I, "end": I, "new": S}, ("path", "start", "end", "new"), "edit",
             handler=lambda c, a: c.ws.edit_lines(a["path"], a["start"], a["end"], a["new"])),
        Tool("write_file", "Create a new file, or overwrite a file you have read.",
             {"path": S, "content": S}, ("path", "content"), "edit",
             handler=lambda c, a: c.ws.write(a["path"], a["content"])),
        Tool("undo_file", "Restore one file to its original content (or delete it if new).",
             {"path": S}, ("path",), "edit", handler=lambda c, a: c.ws.undo(a["path"])),
        Tool("show_diff", "Show your changes so far as a unified diff.", {},
             handler=lambda c, a: c.ws.patch() or "(no changes yet)"),
        Tool("run_check", "Run a registered check (e.g. tests, syntax). `args` is appended, "
             "e.g. a test file path or -k expression.",
             {"name": S, "args": S}, ("name",), "check",
             handler=lambda c, a: c.run_check(a["name"], a.get("args", ""))),
        Tool("instructions_for", "Show persistent AGENTS.md/agent.md instructions that apply to a path.",
             {"path": S}, ("path",), "read",
             handler=lambda c, a: c.project_instructions.context_for(a["path"]) or "(no project instructions)"),
        Tool("list_skills", "List available micro-harness skills and what each is for.", {},
             kind="read", handler=lambda c, a: c.skills.summary()),
        Tool("use_skill", "Activate a focused micro-harness recipe in the current agent. This does not spawn another agent.",
             {"name": S}, ("name",), "read", handler=lambda c, a: c.activate_skill(a["name"])),
        Tool("dev_start", "Start a named local development process using an argv array (no shell). Logs go to run evidence.",
             {"name": S, "argv": {"type": "array", "items": S}, "cwd": S}, ("name", "argv"), "dev",
             handler=lambda c, a: c.dev.start(a["name"], a["argv"], a.get("cwd", "."))),
        Tool("dev_status", "Show status of one or all managed local development processes.",
             {"name": S}, kind="read", handler=lambda c, a: c.dev.status(a.get("name"))),
        Tool("dev_logs", "Tail captured logs from a managed development process.",
             {"name": S, "max_bytes": I}, ("name",), "read",
             handler=lambda c, a: c.dev.logs(a["name"], a.get("max_bytes", 12000))),
        Tool("dev_stop", "Stop a managed local development process.",
             {"name": S}, ("name",), "dev", handler=lambda c, a: c.dev.stop(a["name"])),
    ]
    if allow_shell:
        tools.append(Tool(
            "run_command", "Run a shell command in the project root (bounded time and output).",
            {"command": S, "timeout": I}, ("command",), "check",
            handler=lambda c, a: _shell(c, a)))
    if allow_extract:
        tools += [
            Tool("extract_text", "Get text from a document in the project: txt/html/docx/pdf, "
                 "or images via OCR. Returns cleaned text.",
                 {"path": S}, ("path",),
                 handler=lambda c, a: _doc_text(c, a)),
            Tool("extract_table", "Extract data from documents into rows. `fields` items: "
                 "{name, pattern (regex or @email @money @date @phone @number @id @text ...), "
                 "label (text before the value), type (str|int|float|money|date|percent), "
                 "multiple, required}. tables=true also returns detected tables.",
                 {"paths": {"type": "array", "items": S}, "fields": {"type": "array", "items": {"type": "object"}},
                  "tables": {"type": "boolean"}, "key_values": {"type": "boolean"},
                  "out": {**S, "description": "optional output file .csv/.json/.md in the project"}},
                 ("paths",), "read", handler=_extract_table),
        ]
    tools += [
        Tool("propose_plan", "Submit your plan for approval before making changes.",
             {"goal": S, "steps": {"type": "array", "items": S},
              "files": {"type": "array", "items": S}, "checks": {"type": "array", "items": S}},
             ("goal", "steps"), "control"),
        Tool("finish", "Stop. Summarise what you changed and how you verified it, or why you could not.",
             {"summary": S}, ("summary",), "control"),
    ]
    return {t.name: t for t in tools}


def _shell(ctx, args) -> str:
    timeout = max(1, min(int(args.get("timeout") or ctx.config.command_timeout), ctx.config.command_timeout))
    code, out, timed_out = run_command(args["command"], ctx.ws.repo, timeout)
    status = f"TIMEOUT after {timeout}s" if timed_out else f"exit={code}"
    return f"{status}\n{out}"


def _doc_text(ctx, args) -> str:
    doc = ex.load_document(ctx.ws.path(args["path"]))
    warn = f"warnings: {'; '.join(doc.warnings)}\n" if doc.warnings else ""
    return f"[{doc.method}] {warn}{doc.text}"
