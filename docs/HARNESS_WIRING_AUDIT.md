# Nessa harness wiring audit

Audited against the current working tree on 2026-10-04. This describes Nessa's
implementation, not the private implementation or identity of another AI product.

## Actual desktop route

```text
User message + optional project
  -> saved desktop conversation
  -> one Agent controller
       chat phase: small, streamed local model
       start_work: enter the same controller's project workflow
       plan / execute phase: larger local model
  -> assemble instructions, relevant lessons, skill recipes and session state
  -> validate offered tool name, phase and argument schema
  -> record operation receipt before execution
  -> inspect or edit private workspace / run checks / manage dev process
  -> return observation to model
  -> checkpoint changed files and run continuous checks
  -> independent finish checks; optional advisory review
       failure: return evidence and retry within budget
       accepted: save result, patch and resumable state
```

Fast conversation is not an autonomous subagent. It can answer or request the
project workflow; it cannot approve a plan, execute edit tools in chat phase, or
claim verification on behalf of the controller. The user's original request,
not the small model's paraphrase, remains the project task.

The small model is kept resident by `nessa-chat.service` on port 11436; the
project model remains on port 11435. The extra review model is opt-in and does
not run for greetings. Streaming changes presentation, not verification authority.

## Capability map

| Layer in the requested architecture | Actual wiring | Boundary |
|---|---|---|
| Goal / orchestration | `Agent.run`, chat/plan/execute phases, user follow-ups | One task and an approved plan; no autonomous multi-project goal scheduler |
| Dynamic context | `_intro`, `_system`, `_ask`, project instructions, search/read/outline tools | File search and bounded character budgets; no semantic retrieval index |
| Persistent plan | `propose_plan`, approval/revision, session plan | No separate structured per-step completion ledger |
| Action selection | Model selects only offered tools | Phase, schema, budgets and effects are controlled by Python |
| Tool execution | `tools.py`, `workspace.py`, `checks.py`, `dev.py` | Files, commands, checks, extraction, recipes and processes; external adapters are absent |
| Observation | `_deliver`, matching native call IDs, operation receipts | Full evidence on disk; bounded observations in model context |
| Verification | `_after_edit`, `_finish_gate`, configured checks | Checks measure their configured predicates, not every possible interpretation of the goal |
| Recovery | Failed-check feedback, finish retries, context-overflow retry, stall/budget stops | Bounded repair; not an indefinite daemon or automatic repair after every process crash |
| Project memory | AGENTS.md chain; human-added `LessonStore` notes | Root rules are automatic; nested rules are queried through `instructions_for` |
| Session memory | Atomic `session.json`, messages, digest, receipts, patches | Process-level persistence, not a database transaction across commands and filesystem changes |
| Compaction | Deterministic digest plus recent complete tool exchanges | Includes activated recipe snapshots and lessons; oversized essential state raises an error rather than silently dropping it |
| Skills | Repository `.agent/skills/*`, built-in recipes, `use_skill` | Recipes remain in the parent loop; declared skill checks are guidance, not automatically registered executable checks |
| Review | Optional `Reviewer`, periodic and final advisory notes | No tools or completion authority; disabled by default to preserve local responsiveness |
| Backend separation | Local OpenAI-compatible `ChatClient`; optional chat client in the same Agent | Desktop is wired to fast/large models; CLI keeps its selected model unless configured in Python |
| Development processes | Named start/status/logs/wait/health/stop, config presets, reattach after restart | No interactive PTY; desktop keeps processes until the window closes |
| Telemetry | JSON events, receipts, `timing.json`, OTLP/JSON `trace.json`, optional local OTLP export | No dashboard or cost accounting; prompts/outputs are not exported |
| Benchmarking | Scripted regression tests, `batch.py`, real-model smoke acceptance | Batch output is not a protected holdout evaluator or a promotion decision |
| Recursive improvement | Nessa can propose patches to a harness in a private workspace | No automatic train/benchmark/compare/promote/revert outer loop; no weight training |
| Subagents | Not wired | Intentionally one parent agent loop, per AGENTS.md; parallel benchmark tasks are independent runs |
| Browser / MCP / external services | `[mcp.*]` servers from agentharness.toml, tools `mcp__SERVER__TOOL` | Non-read-only MCP tools need plan approval; Chrome DevTools needs Node.js and is not validated here |

`verified` means the configured finish checks passed with a non-syntax check;
it is not proof of broad agent competence. Missing tests, syntax-only validation,
setup errors, timeouts and reviewer praise do not independently establish success.
The private workspace protects normal edits to original project files; generic
shell commands still have the OS permissions of the account running Nessa.

## Wiring gaps repaired in this audit

1. **Project lessons reached only CLI `run`.** Desktop and terminal chat now
   retrieve lessons for the current project and message. Sessions persist the
   loaded lessons; resume restores them when the caller has not supplied a fresh
   lesson selection. Plain unbound chat does not retrieve another project's notes.
2. **Compaction remembered skill names but not necessarily their recipes.**
   Activation now snapshots the recipe and its source. Those snapshots survive
   compaction and restart; an on-disk recipe edit cannot silently substitute a
   different already-activated procedure. Explicit reactivation refreshes it.
3. **Short tasks could miss advisory review.** A configured reviewer now runs at
   the finish gate as well as at the edit cadence. Review failures are logged
   without overriding deterministic check results. Desktop exposes
   `--review-model` / `--review-base-url`; terminal chat now honors its existing
   reviewer options. Review remains opt-in.

Existing state files remain readable. Legacy sessions cannot recover recipe
snapshots or lessons that were never saved; reactivate a skill or retrieve the
project lessons on a new turn to populate that state.

## Version-bound verification receipts

`agentharness/receipt.py` hashes the workspace patch and the registered check definitions
at the moment the finish checks complete, before acceptance grading or review. The
`verification_receipt` event and `RunResult.receipt` record it. A finish is not accepted
as verified if the workspace no longer matches (`stale_receipt`), and a finish retried
with identical files, checks and failure output stops at once (`repeated_failure`)
instead of spending retries without new evidence. Receipts show which version a check
result applies to; they do not show the checks are sufficient, and the hashes are not a
security boundary against concurrent writers. Tests: `agentharness/tests/test_receipts.py`.

## Evidence and validation

`agentharness/tests/test_architecture_wiring.py` specifically exercises:

- recipe instructions and lessons retained after actual context compaction;
- resumed activated recipes remaining stable when their source files change;
- a one-edit task receiving final review;
- reviewer failure and praise never changing syntax-only work into verified work;
- desktop retrieval of project-specific lessons for the current message.

The wider suite covers fast-model handoff, tool/observation binding, schema
validation, native/text tool formats, approval boundaries, edits/checkpoints,
finish-gate repair, missing/setup-failed checks, unknown-outcome handling,
restart, context budgets and UI flows. These are deterministic harness tests,
not claims about a live model's coding success rate.

Validation on 2026-10-04: the full suite ran 110 tests in 10.225 seconds;
109 passed and one missing-OCR-path test was skipped because OCR is installed.
These results include real local HTTP test servers and native Tk window checks,
with scripted model decisions rather than paid or live-model coding evaluation.

Run the conformance cases with:

```bash
python -m unittest agentharness.tests.test_architecture_wiring \
  agentharness.tests.test_fast_chat agentharness.tests.test_message_loop -v
python -m unittest discover -v
```

Prior measured chat latency evidence is separate, under
`~/nessa-latency/fast-results.json`; it is not an architecture test or a coding benchmark.

## Remaining work before the full proposed stack exists

The core agent loop is implemented. Since this audit, project-defined configuration
(`agentharness.toml`), structural-check registration, executable skill verification
contracts, MCP stdio/HTTP adapters, durable dev-process reconciliation, health probes,
run timing/OTLP traces and desktop patch application have been added (see
`agentharness/tests/test_project_config.py`). The Chrome DevTools adapter was validated headless on 2026-10-05 (page load plus console
capture through `McpBus`), and `python -m agentharness report` renders a run's evidence as
static HTML. Remaining: richer evidence retrieval/semantic search and an independently
scored candidate-promotion loop.

For a future improvement loop, freeze task snapshots and evaluation commands,
score candidates outside their editable workspaces, retain baseline/candidate
identities and latency/resource measurements, and require a controlled promotion
step. Passing the harness's own unit tests is not sufficient to auto-promote it.
