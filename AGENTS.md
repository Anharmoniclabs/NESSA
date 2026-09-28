# Agent instructions

## Purpose

This repository builds a local autonomous coding-agent harness. Keep one parent agent loop.
Skills are focused micro-harness recipes inside that loop; do not introduce nested autonomous
agents unless a task explicitly requires a measured multi-agent experiment.

## Development rules

- Preserve the private-workspace -> patch workflow.
- The model proposes; deterministic harness code owns verification and completion.
- Keep local model backends replaceable.
- Prefer standard-library implementations in the core unless a dependency is clearly justified.
- Every new tool must have a schema, kind, bounded output and tests.
- Every successful edit must remain observable in evidence/checkpoints.
- Do not report setup errors, timeouts, missing tests or reviewer notes as verification success.
- Add focused tests for new harness behavior before broad refactors.

## Current architecture work

See `docs/COMPOSABLE_HARNESS_ARCHITECTURE.md`.

The active integration sequence is:

1. persistent project context
2. composable skills
3. managed local dev processes
4. continuous verification/checkpoints
5. optional independent reviewer
6. durable resume/context digest
7. MCP adapters
8. Chrome DevTools development adapter
9. OpenTelemetry observability
10. structural/architecture conformance checks
