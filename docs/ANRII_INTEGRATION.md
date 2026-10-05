# Local LFM and ANRII integration

The inspected source is [Anharmoniclabs/ANRII](https://github.com/Anharmoniclabs/ANRII)
at commit `0534132007a17899ce57e6e497c3bbb8509f153b`, cloned into
`/home/alabs/Projects/ANRII`. Its standard-library controller patterns were adapted
to Nessa's existing parent loop; ANRII is not a runtime dependency.

| ANRII source | Nessa integration | Live entry point |
|---|---|---|
| `context.py`, `training_bridge.py` | Bounded UTF-8 memory excerpts; current request stays intact; oversized tool evidence remains valid JSON with both output ends and a receipt reference | `memory.ConversationMemory`, `efficient.bounded_evidence`, `Agent._deliver` |
| `models.py::response_schema` | Controller narrows the offered tools for ordinary chat, with `start_work` retaining the full project workflow | `efficient.chat_tools`, `Agent._conversation_round` |
| `_genesis_memory.py`, `learning.py` | Versioned user-authored corrections, provenance hashes, supersession, scoped recall, retained history and deletion from recall | `LessonStore`, desktop `remember KEY: NOTE` / `forget KEY`; desktop and CLI query startup |
| `training_bridge.py::controller_state` | Rebuild query context from the saved visible log; keep the goal and continuation cursor; do not replay obsolete assistant loops | `App.run`, `Agent.run`, `memory-cache.json` |

These are implemented controller behaviors, not imported trained weights. The
optional NEUDO/PyTorch decision models and training system were inspected but are
not loaded. No additional resident model, neural router, cloud distiller, model
download, or autonomous agent is required. The existing private workspace,
approval, receipts and deterministic verification remain responsible for actions.

## Local model routing

Both desktop chat and coding default to `nessa-lfm:latest` at
`http://127.0.0.1:11435/v1`. The launcher starts only `nessa-ollama.service`.
The `local-performance.conf` systemd drop-in keeps LFM resident, preloads it,
allows startup warm-up to finish, and disables Ollama cloud inference.
The experimental cloud gateway and separate Qwen chat service are disabled on
this machine; the cloud integration code and CLI switches have been removed.

Chat has a 640-token generation budget and a 180-second response deadline.
The installed LFM checkpoint emits inline reasoning even when its API reports
thinking disabled. A 192-token cap repeatedly cut off reasoning before the answer.
The stream filter hides inline reasoning, and reasoning-only length exhaustion
stops honestly instead of restarting the same reasoning eight times. The larger
budget allows a final answer; it does not force every reply to use 640 tokens.

The desktop reads the full visible chat log on each query, then packs at most
1600 bytes of retrieved excerpts alongside the current request. Repeated
assistant statements are excluded as examples, while the complete transcript
remains on disk. A fresh query uses this context rather than duplicating the old
internal model conversation. During a query, tool exchanges stay in the loop.
Saved continuation state remains available for unfinished writing.

## Memory

In chat, `remember response_style: Explain directly without an introduction.`
saves a user-authored note. A later command with the same key supersedes it;
`forget response_style` removes it from recall and leaves a tombstone in history.
Project chats use their project scope; plain chats use global scope. Both the
desktop and terminal harness recall the resulting lessons on future queries.
Transcript excerpts remain chat-scoped. Neither mechanism changes model weights.

## Validation

Run `python -m unittest discover -s agentharness/tests` for controller contracts.
Tests cover scoped/versioned memory, deletion, Unicode budgets, refreshed history,
tool routing, complete tool result envelopes, suppression of streamed reasoning,
local-only model selection, resume, private workspaces and verification.

`scripts/smoke_memory.py CHAT_JSON --output DIRECTORY` replays an isolated copy
of the visible conversation and its internal checkpoint against local LFM. It
records replies, first visible token timing and prompt budgets. It fails on errors,
generic refusals and explicit false human-identity claims. These are targeted live
checks, not a claim of general model accuracy or training improvement.

## Results on this machine

The final controller suite ran 169 tests with one skipped and no failures. Live
LFM replay corrected the false human-identity claim, acknowledged the request to
stop repeating introductions, and answered the matrix-multiplication question.
Those three long-history turns took 93.4, 64.8 and 67.5 seconds. A subsequent
no-tool prompt reduced serialized input from roughly 3.2 KB to 2.3 KB but took
77.2 seconds; this is evidence of reduced prompt size, not a measured latency win.
CPU generation and the checkpoint's reasoning remain substantial limits.

The earlier 192-token LFM run repeatedly exhausted its reasoning budget without
answering. The final build uses one sufficient-budget reply or reports a genuine
budget failure; it does not silently loop on that failure. A small isolated
48-token-input probe completed in 19.7 seconds. Performance varies with history
and output length. No general speedup or model-accuracy claim is implied.

Persistent local validation summary:
`~/.agentharness/validation/lfm-anrii-2026-10-04.json`. Both local launchers now
point to this checkout, and the desktop was restarted with LFM.
