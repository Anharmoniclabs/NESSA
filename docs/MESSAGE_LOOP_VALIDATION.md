# Message-loop validation — 2026-10-04

Base: `cb10880453818ec7fb6c8426926d4bb8a4a3b903`.
Environment: Linux, Python 3.12. No Ollama executable/model server available here.

| Check | Result |
|---|---|
| `python -m unittest discover -q` | 62 tests: 61 passed, 1 skipped |
| `python -m unittest discover -s kaggle/gemma4_submission -p 'test_*.py' -q` | 6 passed |
| `python -m compileall -q agentharness scripts` | Passed |
| `bash -n scripts/setup_i3.sh` | Passed |
| `git diff --check` | Passed |
| Real Qwen inference on the i3 laptop | Not run; supplied `scripts/smoke_local.py` performs it locally |

The one skip is an existing missing-Tesseract test: Tesseract is installed in this
environment, so the missing-executable branch does not apply. No test failure is
classified as a skip or success.

The 21 added tests exercise actual private workspace edits, native and text tool
feedback, clarification/resume during planning and execution, appended event
sequences, terminal chat follow-ups, cumulative step limits, context compaction,
matching call/result IDs, deferred calls, malformed argument rejection, process
polling, interrupted call closure, receipt reconciliation, blocked unknown-outcome
replays, command-mutation checkpoints, check setup failures and HTTP error handling.
The laptop-profile HTTP test uses a fake model server but real CLI, files and tests.

All original file bytes were checked against upstream Git blob hashes before
publication. Tests show controller behavior on exercised cases, not general coding
accuracy, production isolation, benchmark superiority or measured laptop throughput.
