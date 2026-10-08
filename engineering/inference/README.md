# Nessa inference engineering

This is the persistent working context for designing and measuring Nessa's
inference behavior. Open the NESSA repository as the project and start here.
The root AGENTS.md points agents to this directory; the scoped AGENTS.md defines
how to keep evidence and decisions consistent across sessions.

## Start here

1. [STATE.md](STATE.md): current work, decisions and next steps.
2. [FINDINGS.md](FINDINGS.md): measured baseline and overhead investigation.
3. [SPEC.md](SPEC.md): proposed engineering requirements and acceptance gates.
4. [Baseline data](baseline.json): portable numeric summary from the first live pilot.

## Implementation map

| Responsibility | Code |
|---|---|
| Planning, execution, context assembly, completion | `agentharness/agent.py` |
| Provider requests and native tool parsing | `agentharness/llm.py` |
| Cloud routing and local fallback | `agentharness/cloud.py` |
| Receipts, checkpoints and durable context | `agentharness/session.py` |
| Verification and timing | `agentharness/checks.py`, `agentharness/telemetry.py` |
| Desktop orchestration and presentation | `agentharness/desktop.py`, `agentharness/gui.py` |
| Live pilot and independent checks | `benchmarks/cloud_harness.py` |

## Validation commands

From the repository root:

```bash
python -m unittest benchmarks.test_cloud_harness agentharness.tests.test_cloud -v
python -m unittest agentharness.tests.test_desktop agentharness.tests.test_gui -v
python -m agentharness doctor --cloud --profile lfm-32k --base-url http://127.0.0.1:11435/v1
python benchmarks/cloud_harness.py --cloud --models zai-org/GLM-5.3 --arms nessa --out /tmp/nessa-candidate-unique
```

The last two commands need network access and consume inference credits. The GUI
tests need a working display; skips are not evidence that the UI passed. Use a new
output directory per live run. Full pilot artifacts live at
`/home/alabs/nessa-cloud-pilot-2026-10-08/` on this machine. The checked-in summary
is portable; raw traces, API usage and source snapshots remain in that directory.

## Inference experiments

[EXPERIMENTS.md](EXPERIMENTS.md) records implemented metrics, live results and
backend capability boundaries. Repeat the bounded probe with a fresh output path:

```bash
python benchmarks/inference.py --cloud --local --out /tmp/nessa-inference-new
python -m unittest agentharness.tests.test_inference agentharness.tests.test_fast_chat -q
```
