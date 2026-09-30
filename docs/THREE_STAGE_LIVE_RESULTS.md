# Local Qwen comparison, 2026-09-30

The reviewed text-intake repair solved **4 of 5** small single-file tasks. The
previous published three-stage version solved **0 of 5** on the same fixtures,
model and budgets. This is one deterministic run per code/task pair, not a broad
coding benchmark or a reliability estimate.

## Exactly what was compared

- Baseline: `e5b2d97993e6f26b6cd62747e57f0fbb31b000c3`
- Tested candidate: local `62d2404d8df4bdac499723cfb5279c78ffda6a1d`
- Published equivalent code: `8442ab38387f9a5ed1206804c234772ba699ba9b`
- Candidate/published tree: `d523c92c9dbe951d2679d9340a0162ada4997354`
- Ollama 0.35.0, Qwen2.5-Coder 1.5B Q4_K_M, CPU with two threads,
  temperature 0, seed 42, 8192-token context, 1024 output-token budget per
  intake/solve request, 256 for respond, text-tool mode

The source-grounding experiment developed after these runs is not included.

| Task | Baseline | Candidate |
|---|---|---|
| Addition smoke reproduction | no_plan | verified |
| Falsey configuration fallback | no_plan | blocked |
| Inclusive range endpoint | no_plan | verified |
| Label whitespace/case normalization | budget_exhausted | verified |
| Even-length median | budget_exhausted | verified |

Across all five runs, baseline used 56 model calls, 45,059 prompt tokens and
213.3 seconds; candidate used 15 calls, 7,128 prompt tokens and 149.1 seconds.
Runtime is specific to this machine/run order; these are not isolated speed
benchmarks. Both success and failure durations are included.

Every original fixture and test hash stayed unchanged. Each of the four
successful patches applied cleanly to a fresh original, passed its original
tests again, and passed 100 independent randomized grading cases. Candidate
configuration fallback made no edits: the model confused a function parameter
with a missing configuration entry. Its blocker was not accepted as success.

## Observed behavior

Baseline repeatedly copied the literal action name `tool_name` or made repeated
invalid reads. Candidate uses compact typed signatures, concrete valid call
examples, bounded recent-action evidence and grounded file-to-read recovery.
It still requires approved file scope, a real edit, and independent verification.

The successful smoke run used three model calls: propose plan, propose edit,
respond. After plan approval, the controller supplied current source to solve;
Qwen proposed the arithmetic change. This describes observable actions, not
private or inferred model reasoning. Response prose remained advisory and could
not change the recorded outcome.

## Reproduce and inspect

The exact synthetic task/source/test inputs, sanitized results, model settings,
fixture SHA-256 hashes, stage action names, patch replay results and independent
grades are in `benchmarks/three_stage_20260930/`. No credentials, raw response
reasoning, host paths or private user data are included in this summary bundle.

Prove the original fixtures fail without running a model:

```bash
python benchmarks/three_stage_20260930/run_cases.py --validate-fixtures
```

Run all five against an already-running local Ollama model:

```bash
python benchmarks/three_stage_20260930/run_cases.py --model qwen2.5-coder:1.5b
```

Use `--code /path/to/other/checkout` to compare another version and `--out` for a
new, empty evidence directory. Default evidence persists under
`$HOME/nessa-three-stage-benchmarks/`. The runner auto-approves only its synthetic
fixture plans, never applies patches to a user's source project, and retains
failed runs. For identical sampling settings, use a local model alias configured
with the parameters above. The runner does not install or download anything.

The reported code also passed 41 focused regression tests and 82 aggregate tests
(one skipped) plus independent review. Trusted project/test code executes under
the invoking user's permissions; disposable verification copies are not an OS
security sandbox. Configuration fallback is now a development case; later fixes
must not present its replay as a previously unseen holdout.
