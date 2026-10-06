"""Command line:  python -m agentharness <command> ...

  run PROJECT "task"      plan, approve, edit a private copy, verify, write a patch
  batch --comp DIR        unattended run over DIR/tasks.jsonl (Kaggle layout)
  extract FILES...        OCR/text + regex fields/tables -> CSV/JSON/Markdown
  config PROJECT [--mcp]  validate agentharness.toml (checks, dev, MCP, telemetry)
  lesson add|list         project-scoped notes injected into future runs
  transcript WORK         print a run's transcript (main agent and subagents)
  doctor                  check the model server and optional tools

Enterprise-style mode (--enterprise): edit the project in place, ask per action
(--permission-mode), the model decides completion (--completion model), subagents.
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
from .checks import CheckResult, CheckRunner, detect_checks
from .llm import ChatClient, ModelError
from .memory import LessonStore
from .reviewer import Reviewer
from .skills import SkillRegistry
from .workspace import ToolError, Workspace
from .profiles import PROFILES, apply_profile
from .session import SessionStore

DEFAULT_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1")  # Ollama
DEFAULT_MODEL = os.environ.get("AGENT_MODEL")


def _local_client(a) -> ChatClient:
    return ChatClient(a.base_url, a.model, max_tokens=a.max_tokens, allow_remote=a.allow_remote,
                      temperature=a.temperature if a.temperature is not None else 0.0,
                      reasoning_effort=a.reasoning_effort)


def _client(a):
    """The local model, or (with --cloud) Hugging Face models falling back to the local model."""
    local = _local_client(a)
    if not getattr(a, "cloud", False):
        return local
    from . import cloud
    models = tuple(m for m in (a.cloud_models or "").split(",") if m) or cloud.DEFAULT_MODELS
    token = cloud.load_token(a.hf_token_file)
    def switch(old, new, errors):
        print(f"[model] {old} unavailable -> using {new}", flush=True)
        for error in errors:
            print(f"        {error[:160]}", flush=True)
    return cloud.FallbackClient(cloud.cloud_clients(token, models, max_tokens=max(a.max_tokens or 0, 4096))
                                + [local], on_switch=switch)


def _config(a, **over) -> AgentConfig:
    """Explicit verification flags win over agentharness.toml; unset ones defer to it."""
    names = lambda text: tuple(v for v in text.split(",") if v)
    chosen = {}
    if a.verify is not None:
        chosen["verify"] = names(a.verify)
    if a.continuous_verify is not None:
        chosen["continuous_verify"] = names(a.continuous_verify)
    if a.full_verify_every_edits is not None:
        chosen["full_verify_every_edits"] = max(0, a.full_verify_every_edits)
    if a.review_every_edits is not None:
        chosen["review_every_edits"] = max(1, a.review_every_edits)
    if getattr(a, "permission_mode", None):
        over.setdefault("permission_mode", a.permission_mode.replace("-", "_"))
    if getattr(a, "completion", None):
        over.setdefault("completion", a.completion)
    if getattr(a, "finish_hooks", None):
        over.setdefault("finish_hooks", names(a.finish_hooks))
    if getattr(a, "no_subagents", False):
        over.setdefault("allow_subagents", False)
    compact = PROFILES[a.profile].get("compact_at_tokens",
                                      6144 if a.profile in ("laptop-i3-12gb", "lfm-i3-12gb") else 22000)
    plan_first = not a.no_plan and not (getattr(a, "direct", False) and not getattr(a, "plan", False))
    return AgentConfig(max_context_chars=a.max_context_chars, tool_output_chars=a.tool_output_chars,
                       compact_at_tokens=compact,
                       max_steps=a.max_steps, plan_first=plan_first, allow_shell=not a.no_shell,
                       tool_mode="text" if a.text_tools else "native",
                       baseline_checks=not a.no_baseline, explicit=tuple(chosen), **chosen, **over)


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


def cli_asker(name: str, args: dict) -> tuple[bool, str, bool]:
    """Per-action permission prompt for --permission-mode default / accept-edits."""
    print(f"\n=== Permission: {name} ===")
    for key, value in args.items():
        text = str(value)
        print(f"  {key}: {text[:600]}{'…' if len(text) > 600 else ''}")
    ans = input("Allow? [y] yes / [a] always for this tool / [n] no / or type feedback: ").strip()
    if ans.lower() in ("y", "yes"):
        return True, "", False
    if ans.lower() in ("a", "always"):
        return True, "", True
    return False, "" if ans.lower() in ("", "n", "no") else ans, False


def _workspace(a, project: Path, work: Path) -> Workspace:
    return Workspace.direct(project, work) if getattr(a, "direct", False) else Workspace.create(project, work)


def _subagent_client(a):
    if not getattr(a, "subagent_model", ""):
        return None
    return ChatClient(a.base_url, a.subagent_model, max_tokens=min(a.max_tokens, 2048),
                      allow_remote=a.allow_remote, temperature=0.0)


def print_event(event, data):
    if event == 'operation_started':
        print(f"[tool] {data['name']}", flush=True)
    elif event == 'check':
        if data.get('phase') == 'baseline':
            print(f"[baseline] {data.get('name')}: {data.get('status')} (before changes)", flush=True)
        else:
            print(f"[check] {data.get('name')}: {data.get('status')}", flush=True)
    elif event == 'model_started':
        print(f"[model] Waiting for {data.get('model') or 'model'}… (Ctrl+C to cancel)", flush=True)
    elif event == 'model':
        print(f"[model] Reply received in {data['seconds']:.1f}s", flush=True)
    elif event == 'subagent_started':
        print(f"[subagent] {data['run_id']} ({data.get('model')}): {data.get('description') or ''}", flush=True)
    elif event == 'subagent_finished':
        print(f"[subagent] {data['run_id']} {data['status']} in {data['seconds']:.1f}s", flush=True)
    elif event == 'permission' and not data.get('allowed'):
        print(f"[denied] {data['name']}", flush=True)
    elif event == 'context_compacted':
        print(f"[context] {data['before']} -> {data['after']} messages", flush=True)


def cmd_run(a) -> int:
    project = Path(a.project).resolve()
    task = Path(a.task_file).read_text() if a.task_file else a.task
    if not task:
        sys.exit("Give a task string or --task-file.")
    work = Path(a.work or Path.home() / ".agentharness" / "runs" /
                f"{project.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    ws = _workspace(a, project, work)
    checks = detect_checks(ws.repo)
    for spec in a.check:
        name, _, command = spec.partition("=")
        checks[name] = command
    lessons = LessonStore().relevant(str(project), task)
    reviewer = None
    if a.review_model:
        review_url = a.review_base_url or a.base_url
        review_client = ChatClient(review_url, a.review_model, max_tokens=min(a.max_tokens, 4096),
                                   allow_remote=a.allow_remote)
        reviewer = Reviewer(review_client)
    agent = Agent(_client(a), ws, config=_config(a, require_approval=not a.auto_approve),
                  checks=CheckRunner(checks), approver=None if a.auto_approve else cli_approver,
                  lessons=lessons, reviewer=reviewer,
                  on_event=print_event if a.progress else None,
                  asker=cli_asker, subagent_client=_subagent_client(a))
    print(f"{'Editing in place' if ws.direct else 'Working copy'}: {ws.repo}\nChecks: {', '.join(checks)}")
    result = agent.run(task)
    print(f"\nStatus: {result.status}  ({result.steps} steps, {result.seconds}s)")
    print("Summary:", result.summary)
    for phase, rows in result.checks.items():
        for brief in rows.values():
            print(f"  {phase}: {brief}")
    print(f"Changed: {', '.join(result.changed_files) or 'nothing'}")
    print(f"Patch + evidence: {result.evidence_dir}")
    if result.patch and a.show_diff:
        print("\n" + result.patch)
    if a.apply and result.patch and ws.direct:
        print("Edits were made in place; nothing to apply.")
    elif a.apply and result.patch:
        if result.status not in ("verified", "unverified"):
            print(f"Not applying: status is {result.status}.")
        elif input(f"Apply patch to {project}? [y/N] ").strip().lower() == "y":
            patch_file = Path(result.evidence_dir) / "patch.diff"
            r = subprocess.run(["git", "apply", "--check", str(patch_file)], cwd=project,
                               capture_output=True, text=True)
            if r.returncode:
                print("Patch does not apply cleanly:", r.stderr)
                return 1
            subprocess.run(["git", "apply", str(patch_file)], cwd=project, check=True)
            print("Applied. Review with `git diff` in your project.")
    return 0 if result.status in OK_STATUSES else 1


OK_STATUSES = ("verified", "unverified", "completed", "no_change", "answered", "awaiting_input")


def cmd_chat(a) -> int:
    """Terminal conversation backed by the same single agent and private workspace."""
    project = Path(a.project).resolve()
    work = Path(a.work or Path.home() / '.agentharness' / 'runs' /
                f"{project.name}-{time.time_ns()}")
    ws = _workspace(a, project, work)
    checks = CheckRunner(detect_checks(ws.repo))
    cfg = _config(a, require_approval=not a.auto_approve, conversational=True)
    client = _client(a)
    reviewer = Reviewer(ChatClient(a.review_base_url or a.base_url, a.review_model,
                        max_tokens=min(a.max_tokens, 4096), allow_remote=a.allow_remote)) if a.review_model else None
    task, resume, last = '', False, None
    subagent_client = _subagent_client(a)
    if ws.direct:
        print(f'NESSA chat | editing {project} in place | mode: {cfg.permission_mode or "plan approval"} '
              f'| /quit to exit\nTranscript: {work / "evidence" / "transcript.jsonl"}')
    else:
        print(f'NESSA chat | private workspace: {work} | /apply writes accepted changes | /quit to exit')
    while True:
        try:
            message = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nSession retained:', work)
            return 0
        if message == '/quit':
            return 0
        if not message:
            continue
        if message == '/apply':
            if ws.direct:
                print('Edits are already in the project (direct mode).')
                continue
            if last is None or last.status not in ('verified', 'unverified', 'completed'):
                print('Nothing to apply: only verified or unverified results can be applied.')
                continue
            if input(f'Write {", ".join(ws.changed_files())} into {project}? [y/N] ').strip().lower() != 'y':
                continue
            try:
                print('Applied:', ', '.join(ws.apply_to(project)) or 'nothing (no unapplied changes)')
            except ToolError as exc:
                print(exc)
            continue
        if not resume:
            task = message
        agent = Agent(client, ws, config=cfg, checks=checks,
                      approver=cli_approver if cfg.require_approval else None,
                      on_event=print_event, reviewer=reviewer,
                      lessons=LessonStore().relevant(str(project), message),
                      asker=cli_asker, subagent_client=subagent_client)
        result = last = agent.run(task, resume=resume, message=message if resume else '')
        print(f'nessa> {result.summary}', flush=True)
        if result.status not in ('answered', 'awaiting_input'):
            print(f'[{result.status}] Evidence: {result.evidence_dir}', flush=True)
        resume = True
        if result.status in ('budget_exhausted', 'cancelled', 'error'):
            print('Session saved. Send another message to continue, or /quit to exit.', flush=True)


def cmd_resume(a) -> int:
    work = Path(a.work).resolve()
    ws = Workspace.open(work)
    if not ws.repo.is_dir() or not ws.baseline.is_dir():
        raise ValueError('Resume requires the existing project/private repo and baseline directories')
    saved = SessionStore(work / 'evidence').load()
    cfg = AgentConfig(**saved['config'])
    if a.extra_steps < 0 or a.extra_seconds < 0:
        raise ValueError('Additional budgets must be non-negative')
    cfg.max_steps += a.extra_steps
    cfg.time_budget += a.extra_seconds
    checks = detect_checks(ws.repo)
    checks.update(saved.get('check_commands', {}))
    for name in saved.get('check_names', []):
        if name not in checks:
            checks[name] = lambda ws, name=name: CheckResult(name, 'error', output='Check implementation unavailable after restart')
    agent = Agent(_client(a), ws, config=cfg, checks=CheckRunner(checks),
                  approver=cli_approver if cfg.require_approval else None, asker=cli_asker)
    result = agent.run(saved['task'], resume=True, message=a.message)
    print(f'Status: {result.status}\n{result.summary}\nPatch + evidence: {result.evidence_dir}')
    return 0 if result.status in OK_STATUSES else 1


def cmd_transcript(a) -> int:
    from .subagents import Transcript
    path = Path(a.work).resolve() / 'evidence' / 'transcript.jsonl'
    rows = Transcript(path).read()
    if not rows:
        print(f'No transcript at {path}')
        return 1
    for row in rows:
        if a.agent and row.get('agent') != a.agent:
            continue
        who = row.get('agent', 'main')
        role = row.get('role', '?')
        if role == 'system' and not a.full:
            continue
        text = str(row.get('content') or '')
        calls = row.get('tool_calls') or []
        if calls:
            text += ' '.join(f"\n  -> {c['function']['name']}({c['function']['arguments'][:200]})" for c in calls)
        if not a.full and len(text) > 800:
            text = text[:800] + ' …'
        label = f"{role}:{row['name']}" if role == 'tool' else role
        print(f"[{who}] {label}: {text}")
    return 0


def cmd_batch(a) -> int:
    from .batch import run_batch
    cfg = _config(a, require_approval=False)
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


def cmd_config(a) -> int:
    from .config import load
    project = Path(a.project).resolve()
    cfg = load(project)
    print(cfg.describe())
    print("registered checks: " + ", ".join(detect_checks(project)))
    if a.mcp and cfg.mcp:
        from .mcp import McpBus
        bus = McpBus(cfg.mcp, project, Path.home() / ".agentharness" / "mcp-logs")
        try:
            print(bus.summary())
            for name, tool in sorted(bus.tools().items()):
                print(f"  {name} [{tool.kind}] {tool.description[:100]}")
        finally:
            bus.close()
        return 1 if bus.errors or cfg.errors else 0
    return 1 if cfg.errors else 0


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
    if a.cloud:
        from . import cloud
        try:
            token = cloud.load_token(a.hf_token_file)
            models = tuple(m for m in (a.cloud_models or "").split(",") if m) or cloud.DEFAULT_MODELS
            for client in cloud.cloud_clients(token, models, max_tokens=8):
                try:
                    client.chat([{"role": "user", "content": "ping"}])
                    print(f"cloud {client.model}: available")
                except ModelError as exc:
                    print(f"cloud {client.model}: UNAVAILABLE ({str(exc)[:160]})")
        except ValueError as exc:
            print(f"cloud: {exc}")
    try:
        models = _local_client(a).models()
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
        sp.add_argument("--profile", choices=PROFILES, default="default")
        sp.add_argument("--model", default=DEFAULT_MODEL)
        sp.add_argument("--temperature", type=float, default=None)
        sp.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high"), default=None)
        sp.add_argument("--max-tokens", type=int, default=None)
        sp.add_argument("--allow-remote", action="store_true", help="permit a non-local model server")
        sp.add_argument("--cloud", action="store_true",
                        help="use Hugging Face cloud models first, falling back to the local model. "
                             "Sends prompts and project content to Hugging Face providers.")
        sp.add_argument("--cloud-models", default=os.environ.get("AGENT_CLOUD_MODELS", ""),
                        help="comma-separated HF model ids in preference order")
        sp.add_argument("--hf-token-file", default=None, help="token file (default: HF_TOKEN or ~/Desktop/HF)")

    def agent_args(sp):
        model_args(sp)
        sp.add_argument("--progress", action="store_true", help="show tool/check events while running")
        sp.add_argument("--max-context-chars", type=int, default=None)
        sp.add_argument("--tool-output-chars", type=int, default=None)
        sp.add_argument("--max-steps", type=int, default=40)
        sp.add_argument("--no-plan", action="store_true", help="skip the plan/approval phase")
        sp.add_argument("--no-shell", action="store_true", help="disable run_command")
        sp.add_argument("--no-baseline", action="store_true", help="skip running tests before changes")
        sp.add_argument("--text-tools", action="store_true", default=None, help="JSON-in-text tool calls (no native tools)")
        sp.add_argument("--verify", default=None,
                        help="checks run when the agent finishes (default syntax,tests or agentharness.toml)")
        sp.add_argument("--continuous-verify", default=None,
                        help="cheap checks run automatically after every successful edit")
        sp.add_argument("--full-verify-every-edits", type=int, default=None,
                        help="run the tests check every N successful edits (default 3); 0 disables")
        sp.add_argument("--review-model", default=os.environ.get("AGENT_REVIEW_MODEL", ""),
                        help="optional second local model used only for advisory code review")
        sp.add_argument("--review-base-url", default=os.environ.get("AGENT_REVIEW_BASE_URL", ""),
                        help="reviewer model server; defaults to --base-url")
        sp.add_argument("--review-every-edits", type=int, default=None,
                        help="ask reviewer for notes every N successful edits")
        sp.add_argument("--direct", action="store_true",
                        help="edit the project in place (a start snapshot keeps diff/undo)")
        sp.add_argument("--plan", action="store_true", help="with --direct: still require plan approval first")
        sp.add_argument("--permission-mode", choices=("plan", "default", "accept-edits", "bypass"), default=None,
                        help="per-action permissions: plan=read-only, default=ask for changes, "
                             "accept-edits=edits free/commands ask, bypass=never ask")
        sp.add_argument("--completion", choices=("checks", "model"), default=None,
                        help="checks: verification decides done (default); model: the model decides")
        sp.add_argument("--finish-hooks", default=None,
                        help="with --completion model: checks that must pass before finish, e.g. tests")
        sp.add_argument("--subagent-model", default=os.environ.get("AGENT_SUBAGENT_MODEL", ""),
                        help="model for subagents (e.g. a small specialist); default: the main model")
        sp.add_argument("--no-subagents", action="store_true", help="disable the agent tool")
        sp.add_argument("--enterprise", action="store_true",
                        help="preset: --direct --permission-mode default --completion model")

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

    chat = sub.add_parser('chat', help='terminal conversation with tools and durable follow-ups')
    chat.add_argument('project')
    chat.add_argument('--work')
    chat.add_argument('--auto-approve', action='store_true')
    agent_args(chat)
    chat.set_defaults(fn=cmd_chat)

    resume = sub.add_parser('resume', help='continue an existing private workspace without replaying tools')
    resume.add_argument('work')
    resume.add_argument('--message', default='', help='answer a clarification or steer the task')
    resume.add_argument('--extra-steps', type=int, default=0)
    resume.add_argument('--extra-seconds', type=float, default=0)
    model_args(resume)
    resume.set_defaults(fn=cmd_resume)

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

    cf = sub.add_parser("config", help="validate a project's agentharness.toml")
    cf.add_argument("project")
    cf.add_argument("--mcp", action="store_true", help="connect configured MCP servers and list their tools")
    cf.set_defaults(fn=cmd_config)

    l = sub.add_parser("lesson", help="project notes for future runs")
    l.add_argument("action", choices=["add", "list"])
    l.add_argument("project")
    l.add_argument("text", nargs="?", default="")
    l.set_defaults(fn=cmd_lesson)

    t = sub.add_parser("transcript", help="print a run's conversation transcript")
    t.add_argument("work", help="the run's work directory")
    t.add_argument("--agent", help="only this agent, e.g. main or sub-01-explore")
    t.add_argument("--full", action="store_true", help="include system prompts and untruncated text")
    t.set_defaults(fn=cmd_transcript)

    d = sub.add_parser("doctor", help="check setup")
    model_args(d)
    d.set_defaults(fn=cmd_doctor)

    a = p.parse_args(argv)
    if hasattr(a, "profile"):
        apply_profile(a)
    if getattr(a, "enterprise", False):
        a.direct = True
        a.permission_mode = a.permission_mode or "default"
        a.completion = a.completion or "model"
    if getattr(a, "direct", False) and not getattr(a, "permission_mode", None):
        a.permission_mode = "default"  # in-place edits always go through per-action permissions
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
