"""Controller: inspect -> plan -> approve -> edit snapshot -> verify -> patch + evidence.

The model proposes; the controller decides what is permitted and what counts as done.
Final statuses (exactly one per run):
    verified        changes made and a real check (tests) passed
    unverified      changes made; no failing check, but nothing beyond syntax could confirm them
    improved        checks still fail, but with fewer failures than before the change
    failed_checks   checks fail after all fix attempts
    no_change       finished without changing anything
    stalled         no progress (repeated non-edit actions or no tool calls)
    budget_exhausted  step or time budget used up
    rejected / no_plan  the plan was not approved / never proposed
    error           the model server or harness failed
    answered / awaiting_input / blocked / cancelled  conversational or interrupted states
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .checks import CheckResult, CheckRunner, detect_checks
from .context import ProjectInstructions
from .dev import DevProcessManager
from .llm import ContextOverflow, ModelError, Reply, ToolCall
from .policy import ActionPolicy, apply_policy
from .skills import SkillRegistry
from .session import SessionStore, atomic_json, bounded_context
from .tools import build_tools
from .workspace import ToolError, Workspace

SYSTEM_PROMPT = """You are an autonomous software engineer working through tools on a private copy \
of a project. Nothing you do touches the user's original files; your changes become a patch they review.

How to work:
1. Locate: use search, outline and list_dir to find the relevant code. Read only the region you need.
2. Understand: read_file the exact lines before changing them.
3. Change: make the smallest correct edit with replace_in_file or edit_lines, in the surrounding style.
4. Verify: run_check (tests, syntax) or run_command for a focused test.
5. Finish: call finish saying what you changed and how you verified it. The controller re-runs the \
checks; if they fail you will be asked to fix them.

Rules:
- Act through a tool call every turn. Use ask_user when an essential answer is missing.
- Use respond for a direct informational answer, blocked to report a dependency, or finish for coding completion.
- A tool error is a message for you: read it and adjust instead of repeating the same call.
- Do not weaken or delete tests to make them pass unless the task says so.
- If you are stuck, call finish and say exactly what blocked you. An honest partial result beats a fake success."""

TEXT_MODE_SUFFIX = """

Call a tool by replying with exactly one JSON object and nothing else:
{"name": "<tool name>", "arguments": {...}}
Available tools:
"""

@dataclass
class AgentConfig:
    max_context_chars: int = 80000
    max_calls_per_turn: int = 8
    max_steps: int = 40            # execute-phase model turns
    plan_steps: int = 10           # read-only turns allowed before propose_plan
    plan_first: bool = True
    require_approval: bool = True  # False: plans are auto-approved (batch / Kaggle)
    replan_limit: int = 2
    explore_budget: int = 12       # non-edit calls in a row before a nudge; 2x stops the run
    finish_retries: int = 2        # times a failed verification sends the agent back to work
    verify: tuple = ("syntax", "tests")
    baseline_checks: bool = True   # run tests once before any change, for comparison
    time_budget: float = 1800
    command_timeout: int = 300
    tool_output_chars: int = 6000
    compact_at_tokens: int = 22000
    tool_mode: str = "native"      # "native" tool calls, or "text" JSON for servers without tools
    allow_shell: bool = True
    allow_extract: bool = True
    continuous_verify: tuple = ("syntax",)  # cheap checks after every successful edit
    full_verify_every_edits: int = 3        # run tests every N edits when available; 0 disables
    checkpoint_every_edit: bool = True
    review_every_edits: int = 2             # only used when a reviewer model is configured


@dataclass
class RunResult:
    status: str
    summary: str
    patch: str
    steps: int
    changed_files: list
    plan: dict | None
    checks: dict = field(default_factory=dict)
    evidence_dir: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Ordered, append-only evidence of everything the run did."""

    def __init__(self, path: Path, callback=None):
        self.callback = callback
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if self.path.exists():
            with self.path.open() as f:
                for line in f:
                    self.seq = max(self.seq, json.loads(line)['seq'])

    def __call__(self, event: str, /, **data):
        self.seq += 1
        with self.path.open("a") as f:
            f.write(json.dumps({"seq": self.seq, "t": round(time.time(), 3), "event": event, **data},
                               default=str) + "\n")
            f.flush()
        if self.callback:
            self.callback(event, data)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n... [{len(text) - limit} chars omitted] ...\n{text[-(limit - head):]}"


def auto_approve(plan: dict) -> tuple[bool, str]:
    return True, ""


class Agent:
    def __init__(self, client, ws: Workspace, *, config: AgentConfig | None = None,
                 checks: CheckRunner | None = None,
                 approver: Callable[[dict], tuple[bool, str]] | None = None,
                 lessons: list[str] = (), policy: ActionPolicy | None = None,
                 evidence_dir: Path | None = None, reviewer=None,
                 skills: SkillRegistry | None = None,
                 project_instructions: ProjectInstructions | None = None, on_event=None):
        self.client = client
        self.ws = ws
        self.config = config or AgentConfig()
        self.checks = checks or CheckRunner(detect_checks(ws.repo), timeout=self.config.command_timeout * 2)
        self.approver = approver or (auto_approve if not self.config.require_approval else None)
        if self.approver is None:
            raise ValueError("require_approval=True needs an approver callback.")
        self.lessons = list(lessons)
        self.policy = policy
        self.reviewer = reviewer
        self.evidence_dir = Path(evidence_dir or ws.work_dir / "evidence")
        self.project_instructions = project_instructions or ProjectInstructions(ws.repo)
        self.skills = skills or SkillRegistry(ws.repo)
        self.dev = DevProcessManager(ws, self.evidence_dir)
        self.log = EventLog(self.evidence_dir / "events.jsonl", on_event)
        self.store = SessionStore(self.evidence_dir)
        self.plan = None
        self.phase = "plan" if self.config.plan_first else "execute"
        self.latest_checks = {}
        self.last_observation = ""
        self.elapsed = 0.0
        self.user_messages = []
        self.plan_terminal = None
        self.uncertain_calls = set()
        self.schema_chars = 0
        self.tools = build_tools(self.config.allow_shell, self.config.allow_extract)
        self.active_skills: list[str] = []
        self.edit_count = 0
        self.task = ""
        self.messages: list[dict] = []
        self._result_idx: list[int] = []
        self._call_idx: list[int] = []
        self._seen_calls: dict[str, int] = {}
        self.generation = 0        # bumps on every edit or command: invalidates duplicate detection
        self.steps = 0
        self.no_progress = 0
        self.finish_attempts = 0
        self.baseline: dict[str, CheckResult] = {}
        self.final: dict[str, CheckResult] = {}
        self.last_tool = ""
        self.started = time.monotonic()

    # ------------------------------------------------------------ tool context

    def run_check(self, name: str, extra: str = "") -> str:
        r = self.checks.run(name, self.ws, extra)
        self.latest_checks[name] = r.brief()
        self.log("check", phase="agent", **{**r.to_dict(), "output": clip(r.output, 2000)})
        return r.brief() + "\n" + r.output

    def activate_skill(self, name: str) -> str:
        try:
            skill = self.skills.get(name)
        except KeyError as exc:
            return f"ERROR: {exc}"
        if name not in self.active_skills:
            self.active_skills.append(name)
        missing = [t for t in skill.tools if t not in self.tools]
        self.log("skill", name=name, source=skill.source, missing_tools=missing)
        note = f"\n\nUnavailable harness tools: {', '.join(missing)}" if missing else ""
        return skill.render() + note

    def _after_edit(self) -> str:
        """Checkpoint and verify continuously after a successful repository mutation."""
        self.edit_count += 1
        reports: list[str] = []
        patch = self.ws.patch()
        if self.config.checkpoint_every_edit:
            checkpoint_dir = self.evidence_dir / "checkpoints"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            path = checkpoint_dir / f"edit-{self.edit_count:04d}-step-{self.steps:04d}.diff"
            path.write_text(patch)
            self.log("checkpoint", edit=self.edit_count, step=self.steps, path=str(path),
                     changed=self.ws.changed_files())

        names = [n for n in self.config.continuous_verify if n in self.checks.checks]
        if (self.config.full_verify_every_edits and
                self.edit_count % self.config.full_verify_every_edits == 0 and
                "tests" in self.checks.checks and "tests" not in names):
            names.append("tests")
        check_briefs = []
        for name in names:
            result = self.checks.run(name, self.ws)
            self.log("check", phase="continuous", edit=self.edit_count,
                     **{**result.to_dict(), "output": clip(result.output, 2000)})
            self.latest_checks[name] = result.brief()
            check_briefs.append(result.brief())
            reports.append(result.brief() + ("\n" + clip(result.output, 2500) if result.output else ""))

        cadence = max(1, int(self.config.review_every_edits or 1))
        if self.reviewer is not None and self.edit_count % cadence == 0:
            review = self.reviewer.review(self.task, patch, "\n".join(check_briefs))
            self.log("review", phase="continuous", edit=self.edit_count, notes=clip(review, 4000))
            reports.append("[reviewer]\n" + review)
        return "\n\n".join(reports)

    # ------------------------------------------------------------ model I/O

    def _system(self, offered=None) -> str:
        if self.config.tool_mode != "text":
            return SYSTEM_PROMPT
        lines = [f"- {t.name}: {t.description} schema=" + json.dumps(t.schema()["function"]["parameters"])
                 for t in self.tools.values() if offered is None or t.name in offered]
        return SYSTEM_PROMPT + TEXT_MODE_SUFFIX + "\n".join(lines)

    def _ask(self, offered: list[str]) -> Reply:
        schemas = None if self.config.tool_mode == 'text' else [self.tools[n].schema() for n in offered]
        self.schema_chars = len(json.dumps(schemas)) if schemas else 0
        self.messages[0]['content'] = self._system(offered)
        self._compact()
        self._save_session('running')
        model_started = time.monotonic()
        try:
            reply = self.client.chat(self.messages, schemas, set(offered))
        except ContextOverflow:
            self._compact(keep_last=2, force=True)
            reply = self.client.chat(self.messages, schemas, set(offered))
        if reply.prompt_tokens > self.config.compact_at_tokens:
            self._compact(force=True)
        if reply.native:
            self.messages.append({"role": "assistant", "content": reply.content or "",
                                  "tool_calls": reply.raw_tool_calls})
            self._call_idx.append(len(self.messages) - 1)
        else:
            self.messages.append({"role": "assistant", "content": reply.content or ""})
        self.log("message", message=self.messages[-1])
        self.log("model", content=clip(reply.content or "", 2000), reasoning=clip(reply.reasoning or "", 2000),
                 calls=[{"name": c.name, "args": c.arguments} for c in reply.tool_calls],
                 native=reply.native, prompt_tokens=reply.prompt_tokens,
                 seconds=round(time.monotonic() - model_started, 3))
        return reply

    def _deliver(self, reply: Reply, results: list[tuple[ToolCall, str]]):
        limit = self.config.tool_output_chars
        if reply.native:
            for call, text in results:
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                      "content": clip(text, limit)})
                self._result_idx.append(len(self.messages) - 1)
                self.log("message", message=self.messages[-1])
        elif results:
            body = "\n\n".join(f"### {c.name} result\n{clip(t, limit)}" for c, t in results)
            self.messages.append({"role": "user", "content": "[tool results]\n" + body})
            self._result_idx.append(len(self.messages) - 1)
            self.log("message", message=self.messages[-1])

        self._save_session('running')

    def _say(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self.log("controller", message=text)
        self.log("message", message=self.messages[-1])

    def _digest(self) -> dict:
        return dict(task=self.task, plan=self.plan, phase=self.phase,
                    project_instructions=self.project_instructions.root_context(),
                    changed_files=self.ws.changed_files(), checks=self.latest_checks,
                    active_skills=self.active_skills, user_messages=self.user_messages, steps=self.steps,
                    remaining_steps=max(0, self.config.max_steps - self.steps),
                    last_observation=self.last_observation,
                    evidence_dir=str(self.evidence_dir),
                    next_step='Inspect current evidence and choose a tool; never replay prior edits blindly.')

    def _save_session(self, status: str) -> None:
        digest = self._digest()
        atomic_json(self.evidence_dir / 'context-digest.json', digest)
        self.store.save(dict(version=1, status=status, task=self.task, plan=self.plan,
            phase=self.phase, user_messages=self.user_messages, config=asdict(self.config), messages=self.messages,
            steps=self.steps, edit_count=self.edit_count, active_skills=self.active_skills,
            latest_checks=self.latest_checks, last_observation=self.last_observation,
            elapsed=self.elapsed + time.monotonic() - self.started,
            finish_attempts=self.finish_attempts, baseline={k:v.to_dict() for k,v in self.baseline.items()},
            journal=self.ws.journal, work_dir=str(self.ws.work_dir),
            model=getattr(self.client, 'model', None), base_url=getattr(self.client, 'base_url', None),
            check_names=self.checks.names(),
            check_commands={k:v for k,v in self.checks.checks.items() if isinstance(v, str)}))

    def _compact(self, keep_last: int = 6, force: bool = False):
        limit = self.config.max_context_chars - self.schema_chars
        if force:
            limit = min(limit, max(1000, len(json.dumps(self.messages, ensure_ascii=False)) * 3 // 4))
        compact = bounded_context(self.messages, self._digest(), limit, keep_last)
        if compact is not self.messages:
            self.log('context_compacted', before=len(self.messages), after=len(compact))
            self.messages = compact
            self._result_idx = []
            self._call_idx = []
            self._seen_calls.clear()  # elided reads must be retrievable again

    # ------------------------------------------------------------ tool dispatch

    def _execute(self, call: ToolCall, offered: list[str]) -> str:
        key = json.dumps([call.name, call.arguments], sort_keys=True, default=str)
        if key in self.uncertain_calls:
            return 'ERROR: this operation has an unknown prior outcome. Inspect evidence; automatic replay is blocked.'
        self._save_session('running')
        tool = self.tools.get(call.name)
        receipt = self.store.begin(call.name, call.arguments, call.id, tool.kind if tool else 'unknown')
        self.log('operation_started', operation_id=receipt['operation_id'], name=call.name)
        out = self._dispatch(call, offered)
        status = 'failed' if out.startswith('ERROR:') else 'completed'
        if out.startswith('exit=') and not out.startswith('exit=0\n'):
            status = 'failed'
        if out.startswith('TIMEOUT'):
            status = 'outcome_unknown'
            self.uncertain_calls.add(key)
        self.store.finish(receipt, out, status)
        self.last_observation = clip(out, 2000)
        self.log('operation_finished', operation_id=receipt['operation_id'], status=status)
        return out

    def _dispatch(self, call: ToolCall, offered: list[str]) -> str:
        tool = self.tools.get(call.name)
        if tool is None or call.name not in offered:
            return f"ERROR: tool {call.name!r} is not available now. Available: {', '.join(offered)}"
        if call.arguments is None:
            return "ERROR: the arguments were not valid JSON. Send a JSON object matching the tool's parameters."
        try:
            args = tool.validate(call.arguments)
        except ToolError as exc:
            return f"ERROR: {exc}"
        key = json.dumps([call.name, args], sort_keys=True, default=str)
        if tool.kind == "read" and tool.cacheable and self._seen_calls.get(key) == self.generation:
            self.no_progress += 1
            return ("(duplicate: you already made this exact call and nothing has changed since. "
                    "Use that result, or do something different.)")
        self._seen_calls[key] = self.generation
        before_patch = self.ws.patch() if tool.kind in ("edit", "check", "dev") else None
        try:
            out = tool.handler(self, args)
        except ToolError as exc:
            out = f"ERROR: {exc}"
        except Exception as exc:  # a tool bug must not kill the run; the model sees it
            out = f"ERROR: {type(exc).__name__}: {exc}"
        self.last_tool = call.name
        edited = before_patch is not None and self.ws.patch() != before_patch
        progressed = edited or tool.kind in ("check", "dev")
        if progressed:
            self.generation += 1
        self.no_progress = 0 if progressed else self.no_progress + 1
        self.log("tool", name=call.name, args={k: clip(str(v), 500) for k, v in args.items()},
                 tool_kind=tool.kind, output=clip(out, 2000))
        if edited:
            followup = self._after_edit()
            if followup:
                out += "\n\n[continuous verification]\n" + followup
        return out

    # ------------------------------------------------------------ phases

    def _intro(self, task: str) -> str:
        parts = [f"Task:\n{task.strip()}", f"Project files:\n{self.ws.list_dir('.', 2)}",
                 f"Registered checks for run_check: {', '.join(self.checks.names()) or 'none'}",
                 "Available micro-harness skills:\n" + self.skills.summary()]
        instructions = self.project_instructions.root_context()
        if instructions:
            parts.append("Persistent project instructions:\n" + instructions)
        if self.baseline:
            parts.append("Before any change: " + "; ".join(r.brief() for r in self.baseline.values()))
        if self.lessons:
            parts.append("Lessons from earlier work on this project:\n" + "\n".join(f"- {l}" for l in self.lessons))
        return "\n\n".join(parts)

    def _plan_round(self) -> dict | None:
        readers = [n for n, t in self.tools.items() if t.kind == "read"]
        idle = 0
        for turn in range(self.config.plan_steps + 1):
            if self.elapsed + time.monotonic() - self.started > self.config.time_budget:
                self.plan_terminal = ('budget_exhausted', 'Time budget used during planning.')
                return None
            controls = ['propose_plan', 'ask_user', 'respond', 'blocked']
            offered = readers + controls if turn < self.config.plan_steps else controls
            if turn == self.config.plan_steps:
                self._say("Time to decide: call propose_plan now.")
            reply = self._ask(offered)
            plan, results = None, []
            for index, call in enumerate(reply.tool_calls):
                if self.plan_terminal or index >= self.config.max_calls_per_turn:
                    results.append((call, 'ERROR: deferred; submit in next turn'))
                    continue
                if call.name in ('ask_user', 'respond', 'blocked'):
                    try:
                        args = self.tools[call.name].validate(call.arguments)
                        status, key = {'ask_user': ('awaiting_input', 'question'),
                                       'respond': ('answered', 'message'),
                                       'blocked': ('blocked', 'reason')}[call.name]
                        self.plan_terminal = (status, args[key])
                        results.append((call, 'Control request recorded.'))
                    except ToolError as exc:
                        results.append((call, f'ERROR: {exc}'))
                elif call.name == "propose_plan":
                    try:
                        plan = self.tools["propose_plan"].validate(call.arguments)
                        results.append((call, "Plan submitted for approval."))
                    except ToolError as exc:
                        results.append((call, f"ERROR: {exc}"))
                else:
                    results.append((call, self._execute(call, offered)))
            self._deliver(reply, results)
            if self.plan_terminal:
                return None
            if plan:
                self.log("plan", plan=plan)
                return plan
            if not reply.tool_calls:
                idle += 1
                if idle >= 3:
                    return None
                self._say("Use the read-only tools to inspect the code, then call propose_plan.")
        return None

    def _approve(self) -> tuple[str | None, dict | None]:
        self._say("First inspect the relevant code with the read-only tools, then call propose_plan "
                  "with concrete steps, the files you will change, and the checks you will run. "
                  "Do not edit anything yet.")
        for attempt in range(self.config.replan_limit + 1):
            plan = self._plan_round()
            if self.plan_terminal:
                return self.plan_terminal[0], None
            if plan is None:
                return "no_plan", None
            ok, feedback = self.approver(plan)
            self.log("approval", approved=ok, feedback=feedback)
            if ok:
                note = f" Reviewer note: {feedback}" if feedback else ""
                self._say("Plan approved. Carry it out now: make the edits, run the checks, "
                          "then call finish." + note)
                return None, plan
            if not feedback or attempt == self.config.replan_limit:
                return "rejected", plan
            self._say(f"The plan was rejected: {feedback}\nRevise it and call propose_plan again.")
        return "rejected", None

    def _finish_gate(self) -> tuple[str | None, str]:
        """Decide whether a `finish` request is accepted. Returns (final status or None, message)."""
        if not self.ws.changed_files():
            return "no_change", "Finished with no changes."
        self.final = {n: (self.checks.run(n, self.ws) if n in self.checks.checks else
                          CheckResult(n, 'no_tests' if n == 'tests' else 'error', output='Check not registered'))
                      for n in self.config.verify}
        for r in self.final.values():
            self.log("check", phase="verify", **{**r.to_dict(), "output": clip(r.output, 2000)})
        self.latest_checks.update({k: v.brief() for k,v in self.final.items()})
        failing = [r for r in self.final.values() if r.status in ('failed', 'timeout', 'setup_error', 'error')]
        if not failing:
            real = (all(r.status == 'passed' for r in self.final.values()) and
                    any(r.name != 'syntax' for r in self.final.values()))
            return ("verified" if real else "unverified"), "Accepted."
        report = "\n\n".join(f"{r.brief()}\n{clip(r.output, 3000)}" for r in failing)
        base = "; ".join(f"{r.brief()}" for r in self.baseline.values())
        if self.finish_attempts < self.config.finish_retries:
            self.finish_attempts += 1
            return None, (f"Not accepted: verification failed.\n{report}\n"
                          + (f"\nBefore your change: {base}\n" if base else "")
                          + "\nFix the cause (or undo_file a change that broke it), then call finish again.")
        return ("improved" if self._improved() else "failed_checks"), "Stopped: verification still failing."

    def _improved(self) -> bool:
        before, after = self.baseline.get("tests"), self.final.get("tests")
        if not before or not after:
            return False
        fb = before.counts.get("failed", 0) + before.counts.get("errors", 0)
        fa = after.counts.get("failed", 0) + after.counts.get("errors", 0)
        return before.status == "failed" and after.status == "failed" and bool(after.counts) and fa < fb

    def _execute_phase(self) -> tuple[str, str]:
        permitted = [n for n in self.tools if n != "propose_plan"]
        idle, nudged = 0, False
        while True:
            if self.steps >= self.config.max_steps:
                return "budget_exhausted", f"Stopped after {self.steps} steps."
            if self.elapsed + time.monotonic() - self.started > self.config.time_budget:
                return "budget_exhausted", "Time budget used up."
            state = {"phase": "execute", "step": self.steps, "no_progress": self.no_progress,
                     "edits": len(self.ws.journal), "last_tool": self.last_tool}
            offered, ranking = apply_policy(self.policy, state, permitted)
            if ranking:
                self.log("policy", mode=self.policy.mode, ranking=ranking, offered=offered)
            reply = self._ask(offered)
            self.steps += 1
            if not reply.tool_calls:
                idle += 1
                if idle >= 3:
                    return "stalled", reply.content or "The model stopped calling tools."
                self._say("Continue by calling a tool. Call finish when the work is done or you are blocked.")
                continue
            idle = 0
            results, terminal = [], None
            for index, call in enumerate(reply.tool_calls):
                if terminal is not None or index >= self.config.max_calls_per_turn:
                    results.append((call, 'ERROR: deferred; submit in the next turn'))
                    continue
                if call.name in ('finish', 'respond', 'ask_user', 'blocked') and call.name in offered:
                    try:
                        args = self.tools[call.name].validate(call.arguments)
                    except ToolError as exc:
                        results.append((call, f'ERROR: {exc}'))
                        continue
                    if call.name == 'finish':
                        status, message = self._finish_gate()
                        if status:
                            terminal = (status, args['summary'])
                    elif call.name == 'respond':
                        if self.ws.changed_files():
                            message = 'ERROR: files changed; use finish so verification runs'
                        else:
                            message = 'Response delivered.'
                            terminal = ('answered', args['message'])
                    elif call.name == 'ask_user':
                        message = 'Waiting for user input; resume with the answer.'
                        terminal = ('awaiting_input', args['question'])
                    else:
                        message = 'Dependency blocker recorded.'
                        terminal = ('blocked', args['reason'])
                    results.append((call, message))
                else:
                    results.append((call, self._execute(call, offered)))
            self._deliver(reply, results)
            if terminal:
                return terminal
            if self.no_progress >= 2 * self.config.explore_budget:
                return "stalled", f"{self.no_progress} actions in a row without a change."
            if self.no_progress >= self.config.explore_budget and not nudged:
                nudged = True
                self._say(f"You have made {self.no_progress} calls without changing anything. Make the "
                          "edit now, or call finish and explain what is blocking you.")
            elif self.no_progress == 0:
                nudged = False

    def run(self, task: str, *, resume: bool = False, message: str = "") -> RunResult:
        self.started = time.monotonic()
        self.task = task
        self.log("resume" if resume else "start", task=task, config=asdict(self.config), checks=self.checks.names(),
                 model=getattr(self.client, "model", None))
        plan, status, summary = None, "error", ""
        try:
            if resume:
                saved = self.store.load()
                self.user_messages = saved.get("user_messages", [])
                if message:
                    self.user_messages.append(message)
                self.task, self.plan, self.phase = saved['task'], saved['plan'], saved['phase']
                self.messages = saved['messages']
                self.steps, self.edit_count = saved['steps'], saved['edit_count']
                self.active_skills = saved['active_skills']
                self.latest_checks = saved['latest_checks']
                self.last_observation = saved['last_observation']
                self.elapsed = saved['elapsed']
                self.finish_attempts = saved['finish_attempts']
                self.baseline = {k: CheckResult(**v) for k,v in saved['baseline'].items()}
                self.ws.journal = saved['journal']
                self.ws._seen.clear()  # require fresh reads after restart
                uncertain = self.store.uncertain()
                self.uncertain_calls = {json.dumps([r['tool_name'], r['arguments']], sort_keys=True, default=str)
                                        for r in uncertain}
                # Unanswered native calls from a crash are closed, never replayed.
                for index in range(len(self.messages) - 1, -1, -1):
                    envelope = self.messages[index]
                    if envelope.get('tool_calls'):
                        following = self.messages[index + 1:]
                        answered = {m.get('tool_call_id') for m in following if m['role'] == 'tool'}
                        insert_at = index + 1
                        while insert_at < len(self.messages) and self.messages[insert_at]['role'] == 'tool':
                            insert_at += 1
                        for call in envelope['tool_calls']:
                            if call['id'] not in answered:
                                self.messages.insert(insert_at, dict(role='tool', tool_call_id=call['id'],
                                    name=call['function']['name'],
                                    content='Outcome unknown after interruption; inspect evidence.'))
                                insert_at += 1
                        break
                self._say('Resumed existing private workspace. Previous process handles are not reattached. '
                          'Read current files before editing. ' + (('User message: ' + message) if message else ''))
                if uncertain:
                    self._say('Uncertain operations (do not repeat without inspection): ' +
                              json.dumps([{'operation_id': r['operation_id'], 'tool': r['tool_name']} for r in uncertain]))
                stop = None
            else:
                if self.config.baseline_checks and 'tests' in self.checks.checks:
                    self.baseline['tests'] = self.checks.run('tests', self.ws)
                    self.log('check', phase='baseline', **self.baseline['tests'].to_dict())
                self.messages = [{'role': 'system', 'content': self._system()},
                                 {'role': 'user', 'content': self._intro(task)}]
                stop = None
            if self.phase == 'plan':
                stop, self.plan = self._approve()
                if not stop:
                    self.phase = 'execute'
            elif not resume:
                self._say('Start now: inspect, edit, verify, then call finish.')
            plan = self.plan
            if stop:
                status, summary = self.plan_terminal or (stop, "No approved plan; nothing was changed.")
            else:
                status, summary = self._execute_phase()
        except (ModelError, ContextOverflow) as exc:
            status, summary = "error", f"Model server error: {exc}"
        except Exception as exc:
            status, summary = "error", f"Harness error: {type(exc).__name__}: {exc}"
        except KeyboardInterrupt:
            status, summary = 'cancelled', 'Interrupted; session saved for resume.'
        self.dev.stop_all()
        patch = self.ws.patch()
        result = RunResult(status, summary, patch, self.steps, self.ws.changed_files(), plan,
                           {"baseline": {k: v.brief() for k, v in self.baseline.items()},
                            "final": {k: v.brief() for k, v in self.final.items()}},
                           str(self.evidence_dir), round(time.monotonic() - self.started, 1))
        (self.evidence_dir / "patch.diff").write_text(patch)
        (self.evidence_dir / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
        (self.evidence_dir / "messages.json").write_text(json.dumps(self.messages, indent=1, default=str))
        (self.evidence_dir / "journal.json").write_text(json.dumps(self.ws.journal, indent=1))
        self._save_session(status)
        self.log("end", status=status, steps=self.steps, changed=result.changed_files)
        return result
