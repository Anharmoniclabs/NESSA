# Cloud harness pilot

Run three small synthetic Python repair tasks through a single-response model
baseline and Nessa's private-workspace agent loop:

```bash
python -m unittest benchmarks.test_cloud_harness -v
python benchmarks/cloud_harness.py --cloud --out /tmp/nessa-cloud-pilot
```

`--models` accepts one or more HF router model IDs. `--arms raw nessa` selects
both setups (the default). The output directory must be new. This makes paid
inference requests using the existing HF token loader. Tokens are never saved.

The task set covers pagination validation, deterministic topological ordering,
and CSV invoice calculations across two files using decimal arithmetic. Each
fixture is checked before inference: broken input must fail and the reference
solution must pass. Reference code and hidden checks remain outside the agent's
workspace and are not included in model requests. Public checks are available
to Nessa's verification loop and included in the raw prompt. The independent
grader applies public and hidden checks after the attempt ends.

Each attempt gets a fresh source and editable workspace. Nessa uses native
tools, automatic plan approval, its standard deterministic completion checks,
16 execution steps, six planning steps and a 240-second controller budget.
Shell, MCP, subagents, external review and persistent lessons are disabled for
this pilot. All remote requests use temperature zero, provider-default reasoning,
4,096 maximum output tokens, one attempt and a 60-second request timeout. The
model-only arm gets one response and must return complete changed files as JSON.
Raw replies may only update the existing task filenames. Order is shuffled with
a fixed seed. The pilot runs sequentially to avoid competing requests.

Production-style local fallback stays in the chain, but any local attempt
disqualifies that run from cloud-only success. Local requests use a short
20-second timeout and 1,024 output tokens; their latency or failure is not a
production local-model performance measurement. Rate limits and other provider
errors are preserved in results. A cloud pass requires an actual remote reply,
passing independent checks, no local fallback, an unchanged source snapshot,
and no runner error. Nessa's own `verified` status is recorded separately.

Outputs include:

- `manifest.json`: requested models, settings and runner checksum.
- `results.json`: every run, independent verdict, latency and API usage.
- Per-run `agent-result.json`, patch, events, receipts and checkpoints for Nessa.
- Per-run `reply.txt` for raw responses and `result.json` for scoring details.

This is a single-repeat integration pilot with tiny synthetic tasks. The raw
and Nessa arms use different amounts of computation. It cannot establish a
general coding score or isolate the benefit of Nessa over other agent loops.
It does not test desktop chat, persistent memory, resume, large repositories,
shell execution, MCP, enterprise completion or subagents. Grader checks run as
ordinary Python processes with a timeout; private workspaces are not a security
sandbox. Provider-reported token usage is recorded; billed cost is not estimated.

A subsequent controlled study needs a basic-agent arm, fixed provider routing,
matched total token/cost budgets, more real repository issues, repetitions and
paired confidence intervals. Evaluate memory and other optional features using
tasks designed to exercise each feature, and disable one feature at a time.

## Inference probes

`python benchmarks/inference.py --cloud --local --out /tmp/nessa-inference-new`
measures first generated/visible events, provider usage, prefix-cache reuse and
one-vs-two-worker cloud batches. Local Ollama reports native prefill/decode times.
Use a fresh output directory. The probe sends 16 bounded cloud generations and
two local generations; it is a serving experiment, not a correctness benchmark.
See `engineering/inference/EXPERIMENTS.md` for measured results and caveats.

The coding pilot now accepts `--stream` to exercise streaming native tools;
omitting it retains the original nonstreaming baseline. Metering captures either
transport. Match this setting explicitly when comparing runs.
