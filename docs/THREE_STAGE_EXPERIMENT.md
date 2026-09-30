# Three-stage experiment v1

Additive experiment based on `cb10880453818ec7fb6c8426926d4bb8a4a3b903`.
The existing `python -m agentharness run` flow and its source files are unchanged.
Use a separate checkout/worktree to preserve local planning/finish patches.

## Run the live-model smoke test

From this checkout:

```bash
bash scripts/run_three_stage_smoke.sh
```

The script uses installed Ollama and the existing `qwen2.5-coder:1.5b` model. It
starts Ollama on `127.0.0.1:11434` if needed; it never installs software or pulls
model weights. Approve the displayed plan if its file scope and checks are right.
`--auto-approve` can be explicitly appended for an unattended fixture run.
`AGENT_MODEL`, `AGENT_BASE_URL` and `NESSA_RUNS_DIR` override defaults.

All runs persist under `$HOME/nessa-three-stage-runs/`. The example stays buggy:
the harness changes a private snapshot and produces a reviewable patch. It does
not apply changes back to the example or your project. The smoke fixture is
ordinary input data, not special-cased in controller code.

For another project:

```bash
python -m agentharness.three_stage /path/to/project 'Repair the described bug' --text-tools
```

Use `--check 'tests=YOUR TRUSTED TEST COMMAND'` to register the relevant check.
Text-tool mode is intended for small local models without reliable native tools.
Native mode is also supported. Both modes reject unknown actions and multiple
calls as a whole turn; neither automatically switches backend/protocol on errors.

## Three enforced stages, one controller

1. **Intake** has only `list_dir`, `search`, `outline`, `read_file`,
   `instructions_for`, and a typed `propose_plan`. A plan must name existing
   files and registered checks. Human approval, or explicit `--auto-approve`,
   grants only that file scope. The controller enforces the approved path list;
   it cannot automatically prove that a natural-language plan matches intent.
2. **Solve** uses a fresh, compact actual-state packet, the approved plan,
   applicable project instructions, current file excerpts, and check evidence.
   It offers true reads, `propose_edit(path, old, new)`, and
   `report_blocker(reason, evidence)`. It does not offer `finish`, shell, dev
   processes, test editing, arbitrary file writes or extraction output writes.
   The controller requires one tool call, validates scope/freshness/unique old
   text/non-no-op replacement, and atomically applies accepted edits. Every
   accepted edit is journaled and checkpointed. A blocker always ends `blocked`.
3. **Respond** is a separate model call with no tools and a 256-token default
   cap. It receives controller facts and asks for a brief result explanation.
   Its output is saved as explicitly advisory in evidence; it cannot override
   the authoritative deterministic summary, checks, patch, or status printed by
   the CLI. False response claims therefore do not become completion evidence.

No hidden reasoning is requested or inferred. Observable evidence includes
model actions, stage inputs, rejected calls, actual patches and check outputs.

## Verification and limits

After each accepted proposal, all required registered checks run independently
in disposable copies of the candidate workspace with the original baseline.
Changes made by verification to visible source/test files invalidate that check.
The candidate workspace is integrity-checked before apply and after response.
A `verified` outcome requires a nonempty approved-scope patch, every required
check passing, and at least one check beyond syntax. Unknown/missing checks,
no-tests, setup errors, timeouts and check exceptions never verify. Passing checks
are evidence for those checks, not proof of universal correctness.

This is **not an OS sandbox for untrusted repositories**. Registered check
commands and imported project code still execute with the invoking user's
permissions. Disposable copies prevent ordinary relative-path test side effects
from changing the proposal workspace, but do not contain malicious absolute-path,
network or process behavior. Use only trusted projects/check commands or run in
a separately isolated environment. Existing symlinks are not approved edit targets.
Known test/config/instruction paths are conservatively protected; the list is not
an exhaustive detector of every language's testing convention.

V1 supports exact replacements in existing regular UTF-8 source files. It does
not support file creation/deletion, test/config edits, new dependencies or shell
work. Large tasks/plans/instructions can hit the context bound and fail honestly.
Model tools/schemas remain backend-dependent; backend failure is recorded as an
error rather than a successful fallback.

Defaults: six inspection turns plus a final plan-only turn, eight solve turns,
three applied proposals, 768 generated tokens per intake/solve call, 256 for the
response, 600 seconds of active stage time and a 180-second request/check timeout.
Approval wait is excluded from active stage time. The stage deadline is checked
before calls and again before any returned action can apply; an in-flight call,
verification and the final response can finish later, bounded by their own
timeouts. This is not a hard end-to-end wall-clock deadline.

## Inspect a run

Open the printed evidence directory:

- `result.json`: authoritative outcome, timings and separately labeled response
- `messages.json`: exact stage packets, offered tools and model outputs
- `events.jsonl`: ordered stages, rejected actions, approvals, proposals and checks
- `journal.json`, `checkpoints/`, `patch.diff`: actual accepted changes

A return code of 0 means `verified`. Every blocked, failed, unavailable or
budget-exhausted run returns nonzero. Finishing honestly with no edit is not a fix.

## Validation versus live-model performance

```bash
python -m unittest agentharness.tests.test_three_stage -q
python -m unittest discover -q
```

The focused suite uses scripted clients plus a local OpenAI-compatible HTTP test
server. It covers the observed premature-edit/false-finish trace, stage separation,
protocol parity, atomic failure, protected paths, no-op/stale proposals, failed
and missing checks, verification mutation, response hallucinations and deadlines.
These tests validate controller behavior. They do not show that Qwen can solve a
repair. Run the live smoke test and inspect its evidence to establish that.

For a useful performance comparison, retain the old guarded path as baseline,
compare finish-removal/fresh-context alone with the full proposal path, and test
several held-out small repair fixtures under the same model/budget. Record valid
proposal rate, correct diff, independent test outcome, blocker rate, model calls
and active latency. Do not tune the controller to this addition fixture.
