# Native Ollama transport experiment

This is an opt-in transport for `python -m agentharness.three_stage`. The default
OpenAI-compatible transport and tool-based solve mode remain unchanged. No model
is installed, downloaded, switched or selected automatically.

Example, after starting an installed local Ollama server and model:

```sh
python -m agentharness.three_stage PROJECT 'TASK' \
  --transport ollama-native --base-url http://127.0.0.1:11434 \
  --model qwen3.5:4b --ollama-think false --text-tools \
  --solve-mode code-only --max-tokens 1024 --timeout 120
```

For the bundled addition smoke fixture, this command reuses the installed model and
starts installed Ollama on loopback if necessary. It still asks for plan approval:

```sh
AGENT_MODEL=qwen3.5:4b AGENT_BASE_URL=http://127.0.0.1:11434 \
  bash scripts/run_three_stage_smoke.sh --transport ollama-native \
  --ollama-think false --solve-mode code-only --max-tokens 1024 --timeout 120
```

The native endpoint is the server origin, **without `/v1`**. Choose an already
installed model and an explicitly advertised thinking mode; the examples select
`qwen3.5:4b` and `false`, not a new default. If the model is absent, the script stops
with an instruction for an explicit pull. It never downloads weights or installs
Ollama. Evidence remains under `$HOME/nessa-three-stage-runs`, and server startup
messages identify the persistent log. Review the model's own license before using
it for a deployment; this transport does not grant rights to model weights.

Code-only restrictions still apply: one approved, existing, syntactically valid
Python file with supported static interfaces. Other tasks can use `--solve-mode tools`.
Approval, scope checks, fresh preimages, independent verification, failure statuses
and a separate response stage are unchanged. This transport does not establish a
model's repair quality; a successful compatibility probe is not a coding benchmark.

## Native contract

- `/api/show` checks the requested `--ollama-think` value against typed
  `thinking.values` metadata. Booleans must be `true` or `false`; named levels must
  match exactly. No silent fallback to another mode, model, endpoint or provider
- Legacy model packages may omit `thinking` metadata while advertising a typed
  `capabilities` list containing `thinking`. Only explicit `true` is accepted in
  that case; the weaker metadata source is recorded. `false` and named levels
  require the explicit values list. Malformed present metadata is rejected
- `/api/chat` receives fresh system/user messages, `stream:false`, the explicit
  thinking control and optional native tool schemas. Ollama uses the installed
  model's own chat template. No `/v1` translation, manual template or response-tag
  stripping is attempted. `truncate:false` and `shift:false` avoid silently
  dropping required prompt context
- Only a loopback HTTP origin is accepted. Redirects and model metadata marked
  `remote_host` or `remote_model` are rejected. This trusts the local server's
  metadata; it is not an OS/network isolation boundary
- Default reproducible options are two CPU threads, 8192-token context, seed 42
  and temperature 0. CLI flags `--ollama-num-thread`, `--ollama-num-ctx` and
  `--ollama-seed` make these explicit; the settings are recorded in evidence

## Budgets and channel separation

`--max-tokens` is capped at 1024 in this experiment. Ollama `num_predict` is a shared
budget for thinking **plus** final output, not an extra thinking allowance. The
response stage retains its smaller 256-token budget. A reasoning model can exhaust
the budget before producing a usable final answer; that is a failed/incomplete
proposal, never an instruction to guess or execute partial output.

The transport retains final `message.content` and validated function calls only.
`message.thinking` is discarded before constructing the controller's `Reply`.
Evidence records its character count/presence, prompt/evaluation/cached-token
counts, finish reason and server duration fields in nanoseconds. It does not
record the private trace or invent separate reasoning-token counts. Final-channel
thinking delimiters, thought-only responses and unknown/incomplete stop states
produce sanitized errors; they are not stripped into executable proposals.

`done_reason:length` drops all final text and tool calls, even if one looks valid.
Reported output beyond the requested token cap is rejected, although a client
cannot undo computation already performed by a server. Truncated response-stage
narration is marked incomplete without changing the controller's authoritative
check-derived result.

Requests are bounded to 2 MiB and have a socket-shutdown elapsed deadline, with no
automatic retries. Deadline-triggered socket failures have the distinct
`OllamaDeadlineExceeded` error type; an early peer disconnect is not relabeled as
a timeout. Error messages never echo HTTP response bodies/status-line
text. A stalled network read cannot authorize an edit. The controller additionally
checks its stage deadline after each model call before applying any action.

## Verification

`python -m unittest agentharness.tests.test_ollama_native -q` uses mock native HTTP
and raw-socket servers. It covers explicit/legacy mode discovery, local-only
metadata, malformed responses, complete and truncated plans/edits, real fixture
checks, thinking omission from all controller artifacts, slow-header/body
trickles, token caps and unchanged originals. These are contract tests, not live
inference results.

Protocol references: [chat endpoint](https://docs.ollama.com/api/chat),
[thinking controls](https://docs.ollama.com/capabilities/thinking),
[Ollama v0.35.0 API types](https://github.com/ollama/ollama/blob/v0.35.0/api/types.go).
