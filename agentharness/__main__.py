"""Command line:  python -m agentharness <command> ...

  run PROJECT "task"      plan, approve, edit a private copy, verify, write a patch
  resume RUN_DIR         continue the same durable working session
  batch --comp DIR        unattended run over DIR/tasks.jsonl (Kaggle layout)
  extract FILES...        OCR/text + regex fields/tables -> CSV/JSON/Markdown
  lesson add|list         project-scoped notes injected into future runs
  doctor                  check the model server and optional tools
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import extract as ex
from .agent import Agent, AgentConfig
from .checks import CheckRunner, detect_checks
from .config import ProjectConfig, load_project_config
from .llm import ChatClient, ModelError
from .memory import LessonStore
from .reviewer import Reviewer
from .session import SessionStore
from .skills import SkillRegistry
from .workspace import Workspace

DEFAULT_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1")  # Ollama
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "qwen2.5-coder:3b")


def _client(a) -> ChatClient:
    return ChatClient(a.base_url, a.model, max_tokens=a.max_tokens, allow_remote=a.allow_remote)


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _config(a, project_config: ProjectConfig | None = None, **over) -> AgentConfig:
    pc = project_config or ProjectConfig()
    verify = _csv(a.verify) or pc.finish_verify or ("syntax", "tests")
    continuous = _csv(a.continuous_verify) or pc.continuous_verify or ("syntax",)
    full_every = (a.full_verify_every_edits if a.full_verify_every_edits is not None
                  else pc.full_verify_every_edits if pc.full_verify_every_edits is not None else 3)
    review_every = (a.review_every_edits if a.review_every_edits is not None
                    else pc.review_every_edits if pc.review_every_edits is not None else 2)
    return AgentConfig(max_steps=a.max_steps, plan_first=not a.no_plan, allow_shell=not a.no_shell,
                       tool_mode="text" if a.text_tools else "native",
                       baseline_checks=not a.no_baseline,
                       verify=verify,
                       continuous_verify=continuous,
                       full_verify_every_edits=max(0, full_every),
                       review_every_edits=max(1, review_every), **over)


def cli_approver(plan: dict) -> tuple[bool, str]:
    print("\n=== Proposed plan ===")
    print("Goal:", plan.get("goal", ""))
    for i, s in enumerate(plan.get("steps", []), 1):
        print(f"  {i}. {s}")
    if plan.get("files"):
        print("Files:", ", ".join(plan["files"]))
    if plan.get("checks"):
        print("Checks:", ", ".join(plan["checks"]))
    ans = input("Approve? [y] yes / [n] no / or type feedback to revise: ").strip()
    if ans.lower() in ("y", "yes"):
        return True, ""
    if ans.lower() in ("", "n", "no"):
        return False, ""
    return False, ans


def _reviewer(a):
    if not a.review_model:
        return None
    review_url = a.review_base_url or a.base_url
    review_client = ChatClient(review_url, a.review_model, max_tokens=min(a.max_tokens, 4096),
                               allow_remote=a.allow_remote)
    return Reviewer(review_client)


def _registered_checks(repo: Path, pc: ProjectConfig, cli_specs: list[str]) -> dict:
    checks = detect_checks(repo)
    checks.update(pc.checks)
    for spec in cli_specs:
        name, sep, command = spec.partition("=")
        if not sep or not name.strip() or not command.strip():
            raise ValueError(f"Bad --check {spec!r}; expected NAME=COMMAND")
        checks[name.strip()] = command.strip()
    return checks


def _print_result(result, *, show_diff: bool = False) -> None:
    print(f"\nStatus: {result.status}  ({result.steps} steps, {result.seconds}s)")
    print("Summary:", result.summary)
    for phase, rows in result.checks.items():
        for brief in rows.values():
            print(f"  {phase}: {brief}")
    print(f"Changed: {', '.join(result.changed_files) or 'nothing'}")
    print(f"Patch + evidence: {result.evidence_dir}")
    if result.patch and show_diff:
        print("\n" + result.patch)


def _maybe_apply(result, project: Path, enabled: bool) -> int:
    if not enabled or not result.patch:
        return 0
    if result.status not in ("verified", "unverified"):
        print(f"Not applying: status is {result.status}.")
        return 0
    if input(f"Apply patch to {project}? [y/N] ").strip().lower() != "y":
        return 0
    patch_file = Path(result.evidence_dir) / "patch.diff"
    r = subprocess.run(["git", "apply", "--check", str(patch_file)], cwd=project,
                       capture_output=True, text=True)
    if r.returncode:
        print("Patch does not apply cleanly:", r.stderr)
        return 1
    subprocess.run(["git", "apply", str(patch_file)], cwd=project, check=True)
    print("Applied. Review with `git diff` in your project.")
    return 0


def cmd_run(a) -> int:
    project = Path(a.project).resolve()
    task = Path(a.task_file).read_text() if a.task_file else a.task
    if not task:
        sys.exit("Give a task string or --task-file.")
    pc = load_project_config(project)
    work = Path(a.work or Path.home() / ".agentharness" / "runs" /
                f"{project.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    ws = Workspace.create(project, work)
    checks = _registered_checks(ws.repo, pc, a.check)
    lessons = LessonStore().relevant(str(project), task)
    store = SessionStore(work, project)
    agent = Agent(_client(a), ws, config=_config(a, pc, require_approval=not a.auto_approve),
                  checks=CheckRunner(checks), approver=None if a.auto_approve else cli_approver,
                  lessons=lessons, reviewer=_reviewer(a), project_config=pc,
                  session=store, source_project=project)
    print(f"Working copy: {ws.repo}\nSession: {work}\nChecks: {', '.join(checks)}")
    if pc.path:
        print(f"Config: {pc.path}")
    result = agent.run(task)
    _print_result(result, show_diff=a.show_diff)
    apply_rc = _maybe_apply(result, project, a.apply)
    if apply_rc:
        return apply_rc
    return 0 if result.status in ("verified", "unverified", "no_change") else 1


def cmd_resume(a) -> int:
    work = Path(a.work).resolve()
    store = SessionStore(work)
    state = store.load()
    if state.get("status") in ("verified", "unverified", "no_change"):
        print(f"Session already completed with status {state['status']}.")
        return 0
    ws = Workspace.open(work)
    source_text = str(state.get("source_project") or "")
    source = Path(source_text).resolve() if source_text else ws.repo
    config_root = source if source.is_dir() else ws.repo
    pc = load_project_config(config_root)
    checks = _registered_checks(ws.repo, pc, a.check)
    task = str(state.get("task") or "")
    lessons = LessonStore().relevant(str(source), task) if task else []
    agent = Agent(_client(a), ws, config=_config(a, pc, require_approval=not a.auto_approve),
                  checks=CheckRunner(checks), approver=None if a.auto_approve else cli_approver,
                  lessons=lessons, reviewer=_reviewer(a), project_config=pc,
                  session=store, source_project=source, resume=True)
    print(f"Resuming: {work}\nWorking copy: {ws.repo}\nChecks: {', '.join(checks)}")
    result = agent.run()
    _print_result(result, show_diff=a.show_diff)
    apply_rc = _maybe_apply(result, source, a.apply) if source.is_dir() else 0
    if apply_rc:
        return apply_rc
    return 0 if result.status in ("verified", "unverified", "no_change") else 1


def cmd_batch(a) -> int:
    from .batch import run_batch
    cfg = _config(a, None, require_approval=False)
    run_batch(_client(a), Path(a.comp), Path(a.out), Path(a.runs), workers=a.workers, config=cfg,
              limit=a.limit, id_key=a.id_key, patch_key=a.patch_key)
    return 0


def cmd_extract(a) -> int:
    spec = ex.load_spec(a.spec) if a.spec else {"fields": []}
    fields = spec["fields"] + [ex.Field.parse(f) for f in a.field]
    files = [p for pattern in a.files for p in (sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[")
                                                   else [Path(pattern)])]
    result = ex.extract(files, fields, tables=a.tables or spec.get("tables", False),
                        kv=a.kv or spec.get("kv", False), dayfirst=a.dayfirst or spec.get("dayfirst", False),
                        lang=a.lang, force_ocr=a.ocr)
    records = result.records
    if a.sum or a.group_by:
        records = ex.aggregate(records, a.group_by, [s for s in (a.sum or "").split(",") if s])
    for doc in result.documents:
        for w in doc.warnings:
            print(f"warning: {doc.source}: {w}", file=sys.stderr)
    if a.out:
        print(f"{len(records)} rows -> {ex.write_rows(records, a.out)}")
    else:
        print(ex.to_markdown(records))
    if result.tables:
        if a.tables_out:
            print(f"{len(result.tables)} table rows -> {ex.write_rows(result.tables, a.tables_out)}")
        else:
            print(ex.to_markdown(result.tables))
    return 0


def cmd_skills(a) -> int:
    registry = SkillRegistry(Path(a.project).resolve())
    print(registry.summary())
    return 0


def cmd_lesson(a) -> int:
    store = LessonStore()
    key = str(Path(a.project).resolve())
    if a.action == "add":
        store.add(key, a.text)
        print("Saved.")
    else:
        for r in store.all(key):
            print(f"- {r['text']}")
    return 0


def cmd_doctor(a) -> int:
    ok = True
    try:
        models = _client(a).models()
        print(f"model server {a.base_url}: reachable; models: {', '.join(models) or '(none)'}")
        if a.model not in models:
            ok = False
            print(f"  ! model {a.model!r} not served. Ollama: `ollama pull {a.model}`")
    except (ModelError, ValueError) as exc:
        ok = False
        print(f"model server {a.base_url}: NOT reachable ({exc})")
    for tool, why in (("git", "applying patches"), ("tesseract", "OCR of images/scans"),
                      ("pdftotext", "PDF text"), ("pdftoppm", "scanned-PDF OCR")):
        print(f"{tool:10} {'found' if shutil.which(tool) else 'missing'}  ({why})")
    try:
        import pytest  # noqa: F401
        print("pytest     found")
    except ImportError:
        print("pytest     missing (tests fall back to unittest)")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentharness", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def model_args(sp):
        sp.add_argument("--base-url", default=DEFAULT_URL)
        sp.add_argument("--model", default=DEFAULT_MODEL)
        sp.add_argument("--max-tokens", type=int, default=4096)
        sp.add_argument("--allow-remote", action="store_true", help="permit a non-local model server")

    def agent_args(sp):
        model_args(sp)
        sp.add_argument("--max-steps", type=int, default=40)
        sp.add_argument("--no-plan", action="store_true", help="skip the plan/approval phase")
        sp.add_argument("--no-shell", action="store_true", help="disable run_command")
        sp.add_argument("--no-baseline", action="store_true", help="skip running tests before changes")
        sp.add_argument("--text-tools", action="store_true", help="JSON-in-text tool calls (no native tools)")
        sp.add_argument("--verify", default=None,
                        help="finish checks (comma-separated); defaults to project config or syntax,tests")
        sp.add_argument("--continuous-verify", default=None,
                        help="checks after every successful edit; defaults to project config or syntax")
        sp.add_argument("--full-verify-every-edits", type=int, default=None,
                        help="run tests every N successful edits; project config or 3 by default; 0 disables")
        sp.add_argument("--review-model", default=os.environ.get("AGENT_REVIEW_MODEL", ""),
                        help="optional second local model used only for advisory code review")
        sp.add_argument("--review-base-url", default=os.environ.get("AGENT_REVIEW_BASE_URL", ""),
                        help="reviewer model server; defaults to --base-url")
        sp.add_argument("--review-every-edits", type=int, default=None,
                        help="ask reviewer every N successful edits; project config or 2 by default")

    r = sub.add_parser("run", help="work on a project")
    r.add_argument("project")
    r.add_argument("task", nargs="?", default="")
    r.add_argument("--task-file")
    r.add_argument("--work", help="work directory (default ~/.agentharness/runs/...)")
    r.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND",
                   help="register a check, e.g. tests='pytest -q tests/unit'")
    r.add_argument("--auto-approve", action="store_true")
    r.add_argument("--show-diff", action="store_true")
    r.add_argument("--apply", action="store_true", help="offer to git-apply the patch to PROJECT")
    agent_args(r)
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="continue an existing durable run")
    rs.add_argument("work", help="existing run directory containing repo/, baseline/ and evidence/session.json")
    rs.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    rs.add_argument("--auto-approve", action="store_true")
    rs.add_argument("--show-diff", action="store_true")
    rs.add_argument("--apply", action="store_true", help="offer to apply a verified patch to the original project")
    agent_args(rs)
    rs.set_defaults(fn=cmd_resume)

    b = sub.add_parser("batch", help="unattended run over tasks.jsonl")
    b.add_argument("--comp", required=True, help="folder with tasks.jsonl and snapshots/")
    b.add_argument("--out", default="predictions.jsonl")
    b.add_argument("--runs", default="agent_runs")
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--limit", type=int)
    b.add_argument("--id-key", default="instance_id")
    b.add_argument("--patch-key", default="model_patch")
    agent_args(b)
    b.set_defaults(fn=cmd_batch)

    e = sub.add_parser("extract", help="documents -> table")
    e.add_argument("files", nargs="+", help="files or glob patterns")
    e.add_argument("--field", action="append", default=[], metavar="NAME[:LABEL]=PATTERN[:TYPE]",
                   help="e.g. 'total:Total Due=@money'  'inv:Invoice No=@id'  'email=@email'")
    e.add_argument("--spec", help="JSON spec file with fields/tables/kv")
    e.add_argument("--tables", action="store_true", help="detect tables")
    e.add_argument("--kv", action="store_true", help="collect generic 'Key: Value' lines")
    e.add_argument("--dayfirst", action="store_true", help="read 03/04/2026 as 3 April")
    e.add_argument("--ocr", action="store_true", help="force OCR for PDFs")
    e.add_argument("--lang", default="eng", help="tesseract language")
    e.add_argument("--group-by")
    e.add_argument("--sum", help="comma-separated fields to total")
    e.add_argument("--out", help=".csv .tsv .json .jsonl .md")
    e.add_argument("--tables-out")
    e.set_defaults(fn=cmd_extract)

    sk = sub.add_parser("skills", help="list built-in and repository micro-harness skills")
    sk.add_argument("project")
    sk.set_defaults(fn=cmd_skills)

    l = sub.add_parser("lesson", help="project notes for future runs")
    l.add_argument("action", choices=["add", "list"])
    l.add_argument("project")
    l.add_argument("text", nargs="?", default="")
    l.set_defaults(fn=cmd_lesson)

    d = sub.add_parser("doctor", help="check setup")
    model_args(d)
    d.set_defaults(fn=cmd_doctor)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
