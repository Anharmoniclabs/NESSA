# Bounded durable code-only resume (opt-in v1)

This is a small recovery path around the published three-stage controller. It
supports **one approved, existing UTF-8 Python source file** and the existing
interface/parse/protected-file gates. The legacy and three-stage CLIs keep their
existing defaults. There is no general tool replay, multi-file transaction,
autonomous shell, process reconnection, or renewed planning on resume.

## Start, resume, cancel

```sh
python -m agentharness.durable start /path/to/project 'Repair addition' \
  --work "$HOME/nessa-durable-runs/addition" --model local-model --text-tools \
  --max-steps 3 --timeout 60 --time-budget 600 --pause-after-approval
python -m agentharness.durable resume "$HOME/nessa-durable-runs/addition" --steps 1
python -m agentharness.durable cancel "$HOME/nessa-durable-runs/addition"
```

Interactive plan approval is the default. `--auto-approve` explicitly authorizes
the initial approval behavior. Resume never asks a new planner to reconstruct or
approve a plan. An interrupted intake/approval without the committed approval
record cannot resume; start a new run in a new work directory.

Native Ollama uses the already published explicit-thinking adapter:

```sh
python -m agentharness.durable start /path/to/project 'Repair addition' \
  --work "$HOME/nessa-durable-runs/native-addition" --transport ollama-native \
  --base-url http://127.0.0.1:11434 --model local-model \
  --ollama-think false --text-tools --max-steps 3
```

The selected thinking mode must be advertised by the model. Native model
metadata is checked again when restoring a nonterminal run. There is no transport
fallback, regenerated credential, copied environment, implicit remote endpoint,
or CLI config override on resume. Runtime restoration uses the exact saved
model, endpoint, token budget, timeout, temperature, native generation options,
and thinking metadata. The native adapter has its existing elapsed deadline;
the OpenAI-compatible adapter retains its existing socket-timeout semantics.
This feature does not add a universal elapsed deadline to that adapter.

`--steps N` is mandatory on every resume and limits **new source-model turns in
that invocation**. An already durable reply/edit/check receipt is reconciled
first and does not consume an extra model turn. Total attempts remain bounded by
the original run's step/proposal limits; repeated resumes cannot replenish them.
A paused run exits 2, verified exits 0, other results/setup refusal exit 1, and a
handled Ctrl-C/SIGINT exits 130. The interruption message does not promise that
an unapproved intake can resume.

Cancel is sticky. It requires the run's lock, so interrupt an active process
before cancelling it. Cancel does not undo an already applied private-workspace
edit. Cancelled and rejected plans never execute on resume. Terminal resume
returns the saved result and makes no model/check calls or evidence writes;
that result describes its original verification time, not a fresh verification.

## Narrow check and source scope

Only the registered `syntax` check plus a predefined `unittest` or `pytest`
argv recipe are supported. Select `--test-runner pytest` only if it is installed
in the same interpreter. `unittest` is the default. No custom command strings,
extra check arguments, callable deserialization, shell interpolation, or new
model tools are exposed. Missing/zero tests and setup failures cannot verify a
run. Each check runs in a fresh disposable source copy, with bounded captured
output and a timeout; its receipt stays associated with the current hashes. A zero exit
code without an executed passing-test count, or an all-skipped suite, is rejected.

Source, baseline and current manifests record SHA-256 and file modes. Protected
tests/instructions and other source files are pinned, not just the editable
file. Git metadata and generated Python/tool caches are excluded. For this
limited release, symlinks, special files, and excluded dependency/build trees
(such as `.venv` or `node_modules`) are refused rather than silently omitted
from a snapshot. Controller module hashes, Python identity, complete config,
check recipes and tool scope are also pinned. A changed/moved original source,
baseline, workspace, check definition, controller or runtime requires a new run
with fresh approval; resume does not guess a replacement configuration.

## Persistence and transitions

State lives outside the generated-code working tree:

```text
work/
  baseline/                  initial source snapshot
  repo/                      private edited snapshot
  session/LOCK               POSIX advisory process lock
  session/state-00000001.json immutable generation
  session/CURRENT.json        atomic generation filename + SHA-256 selector
  evidence/events.jsonl      append-only observation log
  evidence/checkpoints/      edit patch checkpoints
  evidence/result.json       convenience terminal export
```

A generation includes task, exact config, approval, tool/check/model/runtime
contracts, source/baseline/current hashes, stage/next step, total attempts,
active-time accounting, edit journal, check results and bounded operation
receipts. Files are flushed before an atomic selector replacement. A POSIX lock
prevents overlapping start/resume/cancel operations. Start rechecks initialization
under the lock before creating snapshots; it cannot replace an existing run.
Unknown/orphan generations are retained but never automatically promoted.
Missing, corrupt or unsupported selected state fails closed.

| Durable boundary | Explicit resume behavior |
| --- | --- |
| No committed approval | Refuse; no solving or checks |
| Ready for source model | Rebuild bounded context from saved facts and fresh pinned source |
| Model request pending, no durable response | Discard unknown output, charge its already-counted turn and conservative timeout reservation, then respect remaining budgets |
| Full source response durable | Validate saved source through the same parse/interface/scope gates; do not ask the model again |
| Exact replacement pending | If the entire manifest equals preimage, apply once; if it equals the single expected postimage, reconstruct one edit receipt; any other state refuses |
| Edit/checkpoint recorded | Rebuild checkpoint from pinned baseline/current state if needed; verify in disposable copies |
| Checks pending without receipt | Conservatively charge reserved check time and rerun required checks only if time remains |
| Checks recorded | Use the recorded results tied to this exact state; controller alone decides status |
| Advisory response interrupted | Skip narration; never promote narration to authority or replay it |
| Terminal or cancelled | Never execute a new action |

Active time is cumulative across invocations and excludes downtime/approval
waits. Unknown model/check operations charge their configured timeout allowance(s), so a
restart can exhaust time instead of resetting it. These allowances are not
proof of actual elapsed time or a universal upper bound, particularly with the
OpenAI-compatible socket timeout. Proposal and
step budgets never reset. A successful replacement is reconciled before any
further checks/model turns, even if a time budget has since expired.

The selected terminal generation contains the complete result. If interrupted
between terminal commit and convenience-export creation, the authoritative
result remains available through resume; terminal resume intentionally does not
repair exports or rewrite historical evidence. Events are supporting evidence,
not an executable replay log. Malformed existing event logs are refused rather
than truncated.

## What these guarantees do not mean

This is **consistency checking, not a security sandbox, authenticated approval
store, or anti-rollback mechanism**. Someone who can rewrite checksummed state or
select an older consistent generation can also rewrite its authority. The same OS user can alter state, its checksums, code, dependencies or
external files. Tests execute project/generated Python under that user's
privileges. Disposable check copies do not prevent network activity, external
filesystem changes or subprocess side effects. Interrupted checks can therefore
have effects outside the copy; rerunning them is not an exactly-once guarantee.
The narrowly reconciled edit is one existing file replacement, not arbitrary
external effect replay. Environment values and third-party dependency contents
are not frozen. Do not run untrusted projects without separate OS isolation.

Task/source/check output may be private and is retained in generations. The
module intentionally serializes no API keys, credentials or environment dump;
this is not a secret scanner for source text. Keep the work directory private.
No power-loss durability claim is made. Validation covers graceful process
interruption and injected boundaries, not every filesystem/hardware failure.

## Reproduce validation without models

```sh
python -m unittest agentharness.tests.test_durable -q
python scripts/verify_durable_resume.py
python -m unittest discover -s agentharness/tests -q
python -m compileall -q agentharness scripts/verify_durable_resume.py
```

The integration script starts a fake loopback HTTP server, launches the actual
CLI, sends SIGINT while a source response is pending, restarts the process with
`--steps 1`, and confirms one edit, two charged source turns, passing independent
tests, untouched original source and byte-identical terminal no-op. It exercises
both OpenAI-compatible and native transport restoration, including nondefault
native generation options. It performs no real model inference.

## Inspect exported evidence

```sh
python -m agentharness.viewer --runs "$HOME/nessa-durable-runs" --port 8765
```

The viewer is read-only and never resumes or approves a run. It displays exported
results/events, not private durable state generations. If interruption happened
after terminal state commit but before result/end-event export, the viewer remains
`in_progress_or_interrupted`; explicit terminal resume returns the saved result
without recreating those exports. This missing-export case is not new work or a
fresh verification.

Cancellation is stored in the durable session and is enforced by resume, but it
does not emit a separate viewer cancellation artifact. The viewer can therefore
retain its incomplete evidence label after cancellation. Some durable solve and
checkpoint boundaries also lack display events; exported terminal results carry
the final status and changed-file list.
