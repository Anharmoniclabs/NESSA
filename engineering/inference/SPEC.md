# Inference engineering specification

Status: working draft. Requirements below describe intended behavior; implementation
and acceptance evidence are tracked in STATE.md. They do not claim current support.

## Objective

Reduce time and tokens required to produce independently correct work while
preserving tool permissions, durable evidence, local fallback and honest completion.

## Request accounting

Record requested and served model, provider when returned, phase, request duration,
completion reason, input/output tokens and cached/reasoning-token details when
available. Record system, user/history, tool-schema and observation bytes separately.
Missing provider metadata must remain unknown. Never log API keys. Failed requests
and fallback attempts belong in the record even when later attempts succeed.

## Context and tools

Maintain task requirements, applicable project instructions and complete tool-call
exchanges through compaction. Tool selection must preserve access to required
capabilities and controller authority. Independent reads may be grouped; dependent
edits and effects must preserve their ordering and permissions. Context narrowing
must be tested on both small and large repositories.

## Budgets and recovery

Specify per-request output and timeout limits as well as total run time, model
calls and token/cost budgets. Treat reasoning output as part of the provider's
budget accounting. Record retries and exhausted budgets explicitly. A local fallback
may complete the user's task but cannot be counted as a pinned-cloud benchmark win.

## Verification

The agent may run visible checks while working. An independent grader evaluates
the final patch against untouched tests afterward. Keep correctness, the harness's
completion status and infrastructure availability as separate outcomes. A proposed
verification cache must invalidate on relevant code, test, configuration and environment
changes; it may not bypass the final completion gate without separate evidence.

## Evaluation and promotion

Freeze tasks, initial context policy, model/provider settings, repository snapshots
and code revision. Compare raw, basic-agent and Nessa arms. For Nessa-specific
claims match total inference budgets and compare to the basic agent. Use fresh
workspaces and reset lessons between independent trials. Run repetitions in shuffled
order. Report paired outcomes, uncertainty, latency distributions, token totals,
fallback rate and cost only where measured or explicitly estimated.

Proposed initial gate: preserve all current independent pilot passes and controller
regression checks, then show at least 20% lower median input-token use on a larger
held-out task set without worse task success. This is a target, not a measured result.
No automatic promotion from the synthetic pilot alone.

## User interface

Show selected versus actually serving model distinctly and expose fallback failures.
Use consistent light surfaces, readable dark text and restrained status colors.
Keep the composer, model choice and approval actions accessible at 820x640 and on
a 1366x768 screen. Switching conversations and applying UI updates must preserve
drafts and active operations.
