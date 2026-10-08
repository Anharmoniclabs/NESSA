# Measured baseline and overhead

Evidence: 2026-10-08 pilot, 18 attempts, three synthetic Python tasks, one repeat
per model/task/setup. Models: GLM-5.3, DeepSeek-V4-Pro-0813, Kimi-K3. See
baseline.json and `benchmarks/README.md` for settings and limitations.

## Outcomes

| Model | Raw passes | Nessa passes | Raw mean seconds | Nessa mean seconds | Raw total tokens | Nessa total tokens |
|---|---:|---:|---:|---:|---:|---:|
| GLM-5.3 | 3/3 | 3/3 | 7.15 | 22.12 | 2,862 | 77,417 |
| DeepSeek V4 Pro | 2/3 | 3/3 | 29.88 | 35.69 | 7,952 | 106,454 |
| Kimi K3 | Unavailable | Unavailable | — | — | — | — |

Kimi returned HTTP 429 in all six attempts and in a separate Baseten-route probe.
Local fallback was attempted but timed out at the pilot's intentionally short
20-second cap. No local attempt counted as a cloud pass. DeepSeek's raw invoice
attempt returned empty final content at 4,096 completion tokens. Nessa received
multiple response budgets, so its successful result is not a matched-budget
accuracy improvement. Token totals are provider reported, not measured bills.

## Where the overhead comes from

Across the six successful Nessa runs:

- 53 API calls: 25 in planning and 28 in execution, versus six one-shot raw calls.
- Planning consumed 58,743 input tokens; execution consumed 111,784 input tokens.
- Each planning request offered 19 tools; execution offered 25. The full tool
  schemas, instructions and accumulated conversation were sent on each call.
- The runs made 10 directory listings, four searches and three instruction queries,
  even though their initial context already contained a small project tree.
  Some exploration was useful; these counts alone do not prove every call was redundant.
- The system prompt explicitly requests one native tool at a time. The controller
  already accepts multiple calls up to its configured bound, and the traces show
  some models batching reads despite that instruction.
- DeepSeek's invoice run requested `run_check` six times after its edits. Each
  request incurred another model round trip. The mandatory final checks still ran.
- API requests took 170.67 of 173.43 total seconds (about 98.4%). The remaining
  2.76 seconds include checks, files, receipts and controller work. Removing local
  checkpoints would address very little of this measured latency.

The main optimization opportunity is reducing unnecessary model turns and repeated
context. The 27x/13x total-token ratios compare an iterative agent with a tiny
one-response baseline; they are not a pure measure of avoidable harness cost.

## Next experiments

1. Allow batching independent reads while preserving sequential dependent actions.
2. Tell the model that the initial project tree is already available, and to plan
   as soon as it has the evidence needed for the requested scope.
3. Make already-run verification results easier to reuse; keep mandatory final
   verification. Measure repeated-check behavior before adding a check cache.
4. Measure tool-schema bytes separately, then test narrower phase-specific offers
   with a discoverable route to the full authorized tool set.
5. Add a basic-agent baseline and equal total inference budgets before claiming
   Nessa-specific gains. Include real repository issues and repeated trials.

These are proposals. No inference optimization has yet been promoted based on
this investigation. Desktop visual changes are a separate user-facing work item.
