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
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .checks import CheckResult, CheckRunner, detect_checks
from .config import ProjectConfig
from .context import ProjectInstructions
from .dev import DevProcessManager
from .llm import ContextOverflow, ModelError, Reply, ToolCall
from .policy import ActionPolicy, apply_policy
from .session import SessionStore
from .skills import SkillRegistry
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
- Act through a tool call every turn. Nobody will answer questions.
- A tool error is a message for you: read it and adjust instead of repeating the same call.
- Do not weaken or delete tests to make them pass unless the task says so.
- If you are stuck, call finish and say exactly what blocked you. An honest partial result beats a fake success."""

TEXT_MODE_SUFFIX = """

Call a tool by replying with exactly one JSON object and nothing else:
{"name": "<tool name>", "arguments": {...}}
Available tools:
"""

ELIDED = "[older tool output removed to save context; call the tool again if you need it]"


@dataclass
class AgentConfig:
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

    def __init__(self, path: Path, *, append: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if append and self.path.is_file():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    self.seq = max(self.seq, int(row.get("seq") or 0))
            except (OSError, ValueError, TypeError):
                self.seq = 0
        else:
            self.path.write_text("", encoding="utf-8")

    def __call__(self, event: str, /, **data):
        self.seq += 1
        with self.path.open("a") as f:
            f.write(json.dumps({"seq": self.seq, "t": round(time.time(), 3), "event": event, **data},
                               default=str) + "\n")


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
                 project_instructions: ProjectInstructions | None = None,
                 project_config: ProjectConfig | None = None,
                 session: SessionStore | None = None,
                 source_project: Path | None = None,
                 resume: bool = False):
        self.client = client
        self.ws = ws
        self.config = config or AgentConfig()
        self.project_config = project_config or ProjectConfig()
        self.checks = checks or CheckRunner(detect_checks(ws.repo), timeout=self.config.command_timeout * 2)
        self.approver = approver or (auto_approve if not self.config.require_approval else None)
        if self.approver is None:
            raise ValueError("require_approval=True needs an approver callback.")
        self.lessons = list(lessons)
        self.policy = policy
        self.reviewer = reviewer
        self.evidence_dir = Path(evidence_dir or ws.work_dir / "evidence")
        context_limit = self.project_config.context_max_chars or 12000
        self.project_instructions = project_instructions or ProjectInstructions(ws.repo, context_limit)
        self.skills = skills or SkillRegistry(ws.repo)
        self.dev = DevProcessManager(ws, self.evidence_dir)
        self.session = session or SessionStore(ws.work_dir, source_project)
        self.resume_mode = bool(resume)
        self.log = EventLog(self.evidence_dir / "events.jsonl", append=self.resume_mode)
        self.tools = build_tools(self.config.allow_shell, self.config.allow_extract)
        self.active_skills: list[str] = []
        self.edit_count = 0
        self.task = ""
        self.plan: dict | None = None
        self.phase = "created"
        self.status = "running"
        self.messages: list[dict] = []
        self._result_idx: list[int] = []
        self._call_idx: list[int] = []
        self._seen_calls: dict[str, int] = {}
        self.generation = 0        # bumps on every edit or command: invalidates duplicate detection
        self.steps = 0
        self.step_limit = self.config.max_steps
        self.no_progress = 0
        self.finish_attempts = 0
        self.baseline: dict[str, CheckResult] = {}
        self.final: dict[str, CheckResult] = {}
        self.last_tool = ""
        self.started = time.monotonic()
        if self.resume_mode:
            self._restore_session()
            self.step_limit = self.steps + self.config.max_steps

    # ------------------------------------------------------------ durable session

    def _context_digest(self) -> dict:
        return {
            "task": self.task,
            "phase": self.phase,
            "status": self.status,
            "plan": self.plan,
            "steps": self.steps,
            "step_limit": self.step_limit,
            "edit_count": self.edit_count,
            "active_skills": list(self.active_skills),
            "changed_files": self.ws.changed_files(),
            "last_tool": self.last_tool,
            "no_progress": self.no_progress,
            "recent_edits": self.ws.journal[-8:],
            "baseline": {k: v.brief() for k, v in self.baseline.items()},
            "final": {k: v.brief() for k, v in self.final.items()},
        }

    def _persist(self) -> None:
        state = {
            "task": self.task,
            "phase": self.phase,
            "status": self.status,
            "model": getattr(self.client, "model", None),
            "config": asdict(self.config),
            "plan": self.plan,
            "steps": self.steps,
            "edit_count": self.edit_count,
            "generation": self.generation,
            "no_progress": self.no_progress,
            "finish_attempts": self.finish_attempts,
            "active_skills": list(self.active_skills),
            "last_tool": self.last_tool,
            "baseline": {k: v.to_dict() for k, v in self.baseline.items()},
            "final": {k: v.to_dict() for k, v in self.final.items()},
        }
        self.session.save(
            state,
            messages=self.messages,
            digest=self._context_digest(),
            journal=self.ws.journal,
        )

    def _restore_session(self) -> None:
        state = self.session.load()
        self.task = str(state.get("task") or "")
        self.phase = str(state.get("phase") or "execute")
        self.status = "running"
        self.plan = state.get("plan") if isinstance(state.get("plan"), dict) else None
        self.steps = int(state.get("steps") or 0)
        self.edit_count = int(state.get("edit_count") or 0)
        self.generation = int(state.get("generation") or 0)
        self.no_progress = int(state.get("no_progress") or 0)
        self.finish_attempts = int(state.get("finish_attempts") or 0)
        self.active_skills = [str(x) for x in state.get("active_skills") or []]
        self.last_tool = str(state.get("last_tool") or "")
        self.messages = self.session.messages()
        self.ws.journal = self.session.journal()
        for key, row in (state.get("baseline") or {}).items():
            if isinstance(row, dict):
                self.baseline[str(key)] = CheckResult(**row)
        for key, row in (state.get("final") or {}).items():
            if isinstance(row, dict):
                self.final[str(key)] = CheckResult(**row)
        self._result_idx = [
            i for i, message in enumerate(self.messages)
            if message.get("role") == "tool"
            or (message.get("role") == "user"
                and str(message.get("content") or "").startswith("[tool results]"))
        ]
        self._call_idx = [
            i for i, message in enumerate(self.messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        self.session.mark_resume()

    # ------------------------------------------------------------ tool context

    def run_check(self, name: str, extra: str = "") -> str:
        r = self.checks.run(name, self.ws, extra)
        self.log("check", phase="agent", **{**r.to_dict(), "output": clip(r.output, 2000)})
        self._persist()
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
        self._persist()
        note = f"\n\nUnavailable harness tools: {', '.join(missing)}" if missing else ""
        return skill.render() + note

    def dev_profiles(self) -> str:
        return self.project_config.dev_summary()

    def start_dev_profile(self, name: str) -> str:
        profile = self.project_config.dev.get(name)
        if profile is None:
            return f"ERROR: unknown dev profile {name!r}; available: {', '.join(sorted(self.project_config.dev)) or '(none)'}"
        result = self.dev.start(profile.name, list(profile.argv), profile.cwd)
        self.log("dev_profile", name=profile.name, argv=list(profile.argv), cwd=profile.cwd)
        self._persist()
        return result

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
            check_briefs.append(result.brief())
            reports.append(result.brief() + ("\n" + clip(result.output, 2500) if result.output else ""))

        cadence = max(1, int(self.config.review_every_edits or 1))
        if self.reviewer is not None and self.edit_count % cadence == 0:
            review = self.reviewer.review(self.task, patch, "\n".join(check_briefs))
            self.log("review", phase="continuous", edit=self.edit_count, notes=clip(review, 4000))
            reports.append("[reviewer]\n" + review)
        self._persist()
        return "\n\n".join(reports)

    # ------------------------------------------------------------ model I/O

    def _system(self) -> str:
        if self.config.tool_mode != "text":
            return SYSTEM_PROMPT
        lines = [f"- {t.name}({', '.join(t.params)}): {t.description}" for t in self.tools.values()]
        return SYSTEM_PROMPT + TEXT_MODE_SUFFIX + "\n".join(lines)

    def _ask(self, offered: list[str]) -> Reply:
        schemas = None if self.config.tool_mode == "text" else [self.tools[n].schema() for n in offered]
        try:
            reply = self.client.chat(self.messages, schemas, set(offered))
        except ContextOverflow:
            self._compact(keep_last=2)
            reply = self.client.chat(self.messages, schemas, set(offered))
        if reply.prompt_tokens > self.config.compact_at_tokens:
            self._compact()
        if reply.native:
            self.messages.append({"role": "assistant", "content": reply.content or "",
                                  "tool_calls": reply.raw_tool_calls})
            self._call_idx.append(len(self.messages) - 1)
        else:
            self.messages.append({"role": "assistant", "content": reply.content or ""})
        self.log("model", content=clip(reply.content or "", 2000), reasoning=clip(reply.reasoning or "", 2000),
                 calls=[{"name": c.name, "args": c.arguments} for c in reply.tool_calls],
                 native=reply.native, prompt_tokens=reply.prompt_tokens)
        self._persist()
        return reply

    def _deliver(self, reply: Reply, results: list[tuple[ToolCall, str]]):
        limit = self.config.tool_output_chars
        if reply.native:
            for call, text in results:
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                      "content": clip(text, limit)})
                self._result_idx.append(len(self.messages) - 1)
        elif results:
            body = "\n\n".join(f"### {c.name} result\n{clip(t, limit)}" for c, t in results)
            self.messages.append({"role": "user", "content": "[tool results]\n" + body})
            self._result_idx.append(len(self.messages) - 1)
        self._persist()

    def _say(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self.log("controller", message=text)
        self._persist()

    def _compact(self, keep_last: int = 6):
        for i in self._result_idx[:-keep_last]:
            self.messages[i]["content"] = ELIDED if self.messages[i]["role"] == "tool" \
                else "[tool results]\n" + ELIDED
        for i in self._call_idx[:-keep_last]:
            for c in self.messages[i].get("tool_calls") or []:
                fn = c.get("function") or {}
                if len(str(fn.get("arguments", ""))) > 1500:
                    fn["arguments"] = json.dumps({"elided": "large arguments removed"})
        digest = self._context_digest()
        self.messages.append({
            "role": "user",
            "content": "[controller-generated durable state digest]\n"
                       + json.dumps(digest, indent=1, default=str)[:6000],
        })
        self.log("compact", keep_last=keep_last, digest=digest)
        self._persist()

    # ------------------------------------------------------------ tool dispatch

    def _execute(self, call: ToolCall, offered: list[str]) -> str:
        tool = self.tools.get(call.name)
        if tool is None or call.name not in offered:
            return f"ERROR: tool {call.name!r} is not available now. Available: {', '.join(offered)}"
        if call.arguments is None:
            return "ERROR: the arguments were not valid JSON. Send a JSON object matching the tool's parameters."
        try:
            args = tool.validate(dict(call.arguments))
        except ToolError as exc:
            return f"ERROR: {exc}"
        key = json.dumps([call.name, args], sort_keys=True, default=str)
        if tool.kind == "read" and self._seen_calls.get(key) == self.generation:
            self.no_progress += 1
            return ("(duplicate: you already made this exact call and nothing has changed since. "
                    "Use that result, or do something different.)")
        self._seen_calls[key] = self.generation
        try:
            out = tool.handler(self, args)
        except ToolError as exc:
            out = f"ERROR: {exc}"
        except Exception as exc:  # a tool bug must not kill the run; the model sees it
            out = f"ERROR: {type(exc).__name__}: {exc}"
        self.last_tool = call.name
        edited = tool.kind == "edit" and out.startswith("ok")
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
            offered = readers + ["propose_plan"] if turn < self.config.plan_steps else ["propose_plan"]
            if turn == self.config.plan_steps:
                self._say("Time to decide: call propose_plan now.")
            reply = self._ask(offered)
            plan, results = None, []
            for call in reply.tool_calls:
                if call.name == "propose_plan":
                    try:
                        plan = self.tools["propose_plan"].validate(dict(call.arguments or {}))
                        results.append((call, "Plan submitted for approval."))
                    except ToolError as exc:
                        results.append((call, f"ERROR: {exc}"))
                else:
                    results.append((call, self._execute(call, offered)))
            self._deliver(reply, results)
            if plan:
                self.log("plan", plan=plan)
                self._persist()
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
            if plan is None:
                return "no_plan", None
            ok, feedback = self.approver(plan)
            self.log("approval", approved=ok, feedback=feedback)
            if ok:
                self.plan = plan
                self.phase = "execute"
                note = f" Reviewer note: {feedback}" if feedback else ""
                self._say("Plan approved. Carry it out now: make the edits, run the checks, "
                          "then call finish." + note)
                self._persist()
                return None, plan
            if not feedback or attempt == self.config.replan_limit:
                return "rejected", plan
            self._say(f"The plan was rejected: {feedback}\nRevise it and call propose_plan again.")
        return "rejected", None

    def _finish_gate(self) -> tuple[str | None, str]:
        """Decide whether a `finish` request is accepted. Returns (final status or None, message)."""
        self.phase = "verify"
        self._persist()
        if not self.ws.changed_files():
            return "no_change", "Finished with no changes."
        self.final = {n: self.checks.run(n, self.ws) for n in self.config.verify if n in self.checks.checks}
        for r in self.final.values():
            self.log("check", phase="verify", **{**r.to_dict(), "output": clip(r.output, 2000)})
        failing = [r for r in self.final.values() if r.status in ("failed", "timeout")]
        if not failing:
            real = any(r.status == "passed" and r.name != "syntax" for r in self.final.values())
            self.phase = "finalize"
            self._persist()
            return ("verified" if real else "unverified"), "Accepted."
        report = "\n\n".join(f"{r.brief()}\n{clip(r.output, 3000)}" for r in failing)
        base = "; ".join(f"{r.brief()}" for r in self.baseline.values())
        if self.finish_attempts < self.config.finish_retries:
            self.finish_attempts += 1
            self.phase = "execute"
            self._persist()
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
        return before.status == "failed" and fa < fb

    def _execute_phase(self) -> tuple[str, str]:
        permitted = [n for n in self.tools if n != "propose_plan"]
        idle, nudged = 0, False
        while True:
            if self.steps >= self.step_limit:
                return "budget_exhausted", f"Stopped after {self.steps} steps."
            if time.monotonic() - self.started > self.config.time_budget:
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
            results, finish_call = [], None
            for call in reply.tool_calls:
                if call.name == "finish" and "finish" in offered:
                    finish_call = call
                else:
                    results.append((call, self._execute(call, offered)))
            if finish_call:
                status, message = self._finish_gate()
                results.append((finish_call, message))
                self._deliver(reply, results)
                if status:
                    summary = (finish_call.arguments or {}).get("summary", "")
                    return status, summary
                self.no_progress = 0
                continue
            self._deliver(reply, results)
            if self.no_progress >= 2 * self.config.explore_budget:
                return "stalled", f"{self.no_progress} actions in a row without a change."
            if self.no_progress >= self.config.explore_budget and not nudged:
                nudged = True
                self._say(f"You have made {self.no_progress} calls without changing anything. Make the "
                          "edit now, or call finish and explain what is blocking you.")
            elif self.no_progress == 0:
                nudged = False

    def run(self, task: str | None = None) -> RunResult:
        if self.resume_mode:
            if task and self.task and task != self.task:
                raise ValueError("A resumed session keeps its original task.")
            self.task = self.task or (task or "")
            if not self.task:
                raise ValueError("Resumable session has no task.")
            self.status = "running"
            self.log("resume", task=self.task, steps=self.steps, edits=self.edit_count,
                     changed=self.ws.changed_files())
            if not self.messages:
                self.messages = [{"role": "system", "content": self._system()},
                                 {"role": "user", "content": self._intro(self.task)}]
            self._say("Resume the existing session. The current working copy and durable state are "
                      "authoritative. Re-read a file before editing it, continue from the unfinished "
                      "work, verify, and call finish when complete.")
        else:
            if not task:
                raise ValueError("A new run needs a task.")
            self.task = task
            self.phase = "baseline"
            self.status = "running"
            self.log("start", task=task, config=asdict(self.config), checks=self.checks.names(),
                     model=getattr(self.client, "model", None))
        status, summary = "error", ""
        try:
            if not self.resume_mode:
                if self.config.baseline_checks and "tests" in self.checks.checks:
                    self.baseline["tests"] = self.checks.run("tests", self.ws)
                    self.log("check", phase="baseline",
                             **{**self.baseline["tests"].to_dict(), "output": clip(self.baseline["tests"].output, 2000)})
                self.messages = [{"role": "system", "content": self._system()},
                                 {"role": "user", "content": self._intro(self.task)}]
                self._persist()
            stop = None
            if self.config.plan_first and self.plan is None:
                self.phase = "plan"
                self._persist()
                stop, self.plan = self._approve()
            elif not self.resume_mode and not self.config.plan_first:
                self.phase = "execute"
                self._say("Start now: inspect, edit, verify, then call finish.")
            else:
                self.phase = "execute"
                self._persist()
            if stop:
                status, summary = stop, "No approved plan; nothing was changed."
            else:
                status, summary = self._execute_phase()
        except (ModelError, ContextOverflow) as exc:
            status, summary = "error", f"Model server error: {exc}"
        except Exception as exc:
            status, summary = "error", f"Harness error: {type(exc).__name__}: {exc}"
        self.dev.stop_all()
        self.status = status
        self.phase = "completed" if status in ("verified", "unverified", "no_change") else "stopped"
        patch = self.ws.patch()
        result = RunResult(status, summary, patch, self.steps, self.ws.changed_files(), self.plan,
                           {"baseline": {k: v.brief() for k, v in self.baseline.items()},
                            "final": {k: v.brief() for k, v in self.final.items()}},
                           str(self.evidence_dir), round(time.monotonic() - self.started, 1))
        (self.evidence_dir / "patch.diff").write_text(patch)
        (self.evidence_dir / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
        self._persist()
        self.log("end", status=status, steps=self.steps, changed=result.changed_files)
        self._persist()
        return result
