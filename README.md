# NESSA

Local-first autonomous coding agent work.

| Folder | What it is |
|---|---|
| [`agentharness/`](agentharness/README.md) | Coding agent harness: inspect → plan → approve → edit a private copy → verify → patch + evidence. Also includes OCR/regex document extraction into tables. Works with Ollama or vLLM, standard library only. |
| [`kaggle/gemma4_submission/`](kaggle/gemma4_submission/README.md) | Google Gemma 4 Developer Agent competition: declarative `submission/` for the `swegemma` harness, a validator/packager, and Kaggle notebook cells (start server → write submission → local eval). |

```bash
python -m unittest discover -v  # harness tests; no GPU or pytest needed
python -m unittest discover -s kaggle/gemma4_submission -v
python -m agentharness doctor
```

## Message-driven local agent

NESSA now supports terminal chat, durable follow-ups/resume, operation receipts,
context digests, live process polling and stricter verification completion.

- [Message and tool workflow, diagrams and implementation boundaries](docs/MESSAGE_TOOL_WORKFLOW.md)
- [i3 / 12 GB laptop setup and real-model acceptance](docs/LAPTOP_I3_12GB.md)

```bash
bash scripts/setup_i3.sh
python -m agentharness chat /path/to/project --profile laptop-i3-12gb
```
