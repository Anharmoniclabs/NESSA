# Native recurrent-model inference

**Status: extension interface only. Live native inference is UNVERIFIED.** No recurrent
checkpoint/backend with accessible authoritative inference documentation was verified, so NESSA
ships no backend-specific integration, cites no supported versions, and makes no claim of
improved capability or speed. Everything below is tested against fake HTTP servers only.

## Native recurrence vs. agent refinement

* **Native recurrence**: the *backend* runs extra recurrent steps over its own hidden state inside
  **one** inference request. The backend owns hidden states, recurrent layers and recurrence-aware
  caches. NESSA only sends a documented request field and (optionally) reads a reported count.
* **Agent refinement**: the harness loop makes further model calls (plan/edit/verify turns). These
  are budgeted (`--max-steps`, time budget) and reported separately (`agent_passes` in `timing.json`).

One logical chat request is always one HTTP request; recurrence never becomes repeated calls.
(Existing behaviour is unchanged: a context-overflow rejection still triggers one compacted retry
by the agent, and the transport still retries connection failures per `retries`.)
NESSA does not patch ordinary transformer models. Nothing is inferred from a model name or from
"OpenAI compatibility": the default is unsupported.

## Backend requirements

A backend must document (a) a request field that selects recurrence depth, (b) which depths it
supports, and (c) optionally a response field with the steps actually executed. The operator
copies these into a TOML file (`configs/recurrence-eval.example.toml` is a placeholder template):

```toml
[recurrence]
adapter = "field-mapping"                 # built-in generic adapter
[recurrence.options]
request_field = "recurrence.steps"        # dotted path in the request body
usage_field = "usage.recurrence_steps"    # dotted path in the response; omit if not reported
[recurrence.capability]
native_recurrence = true
steps = [1, 2, 4]                         # or: min_steps = 1 / max_steps = 8
reports_actual_usage = true
backend_id = "my-backend"
```

`enabled`/`steps` may also be set in the file. Unknown keys, booleans, non-integers, `0`,
negative or unsupported steps, an undeclared capability, an unknown adapter, a request field that
collides with a standard chat field, or inconsistent usage declarations raise `RecurrenceError`
**before any request** (CLI exit code 2, no workspace created).

## Commands

```
python -m agentharness run PROJECT "task" --recurrence-config rec.toml --recurrence-steps 2
python -m agentharness chat PROJECT --recurrence-config rec.toml --recurrence-steps 2
python -m agentharness batch ... --recurrence-config rec.toml --recurrence-steps 2
python -m agentharness resume WORK            # restores the saved recurrence settings
python -m agentharness recurrence-plan configs/recurrence-eval.example.toml
python -m agentharness recurrence-report results.json
```

Without both flags nothing changes and no recurrence field is sent. `--recurrence-steps` without
a capability file is rejected. `resume` keeps the original settings (max-steps/time budgets are
cumulative as before); passing different or newly enabled settings is rejected. The review model
and the desktop app/profiles never enable recurrence. Programmatic use:
`ChatClient(url, model, recurrence=RecurrenceConfig(...))`.

## Writing an adapter for a documented backend

Subclass `RecurrenceAdapter` (`request_fields(steps) -> dict`, `reported_steps(response)`,
`can_report_usage`) and call `register_adapter(name, factory)`. Fields may not overwrite standard
chat fields.

## Evidence and telemetry

Each `model` event in `events.jsonl` carries `recurrence`: `requested_steps`, `actual_steps`
(`null` unless the capability declares reporting and the backend returned a non-negative integer),
`backend`, `adapter`, `model`, `latency_s`. `session.json` and the `start`/`resume` events record
the settings (no credentials or tensors; hidden states and reasoning are never logged by this
feature). `timing.json` has `agent_passes` and, when used, `native_recurrence`
(`requested_steps_total`, `actual_steps_total` or `null`, `actual_unknown_calls`, `latency_s`);
OTLP model spans get `nessa.recurrence_*` attributes.

## Evaluation

`[evaluation]` in the TOML lists native depths; `recurrence-plan` validates each against the
capability and emits one arm per depth plus an optional recurrence-off baseline, all with
identical `agent_max_steps`, `time_budget`, `max_tokens`. This is only an arm planner and summary
helper (`recurrence-report` takes `{arm: [{status, truth, seconds, prompt_tokens?,
recurrence_requested?, recurrence_actual?}]}` and reports verified completion, false success,
latency, tokens and recurrence usage); it does not duplicate a task runner. No gains are claimed
without measurements from such a run.

## Tests

```
python -m unittest discover -s agentharness/tests -t .
python -m unittest agentharness.tests.test_recurrence
```

Opt-in real-backend smoke test (skipped with a reason otherwise):

```
NESSA_RECURRENCE_SMOKE_URL=http://127.0.0.1:8000/v1 NESSA_RECURRENCE_SMOKE_MODEL=... \
NESSA_RECURRENCE_SMOKE_CONFIG=rec.toml NESSA_RECURRENCE_SMOKE_STEPS=2 \
python -m unittest agentharness.tests.test_recurrence.RealBackendSmoke
```

## Limitations

Unverified against any real backend; no concrete adapter; streaming actual-step reporting relies
on the backend placing the field in a chunk (merged by key); project `agentharness.toml` has no
`[recurrence]` section (use `--recurrence-config`).
