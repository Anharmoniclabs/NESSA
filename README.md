# NESSA

Local-first autonomous coding agent work.

| Folder | What it is |
|---|---|
| [`agentharness/`](agentharness/README.md) | Coding agent harness: inspect → plan → approve → edit a private copy → verify → patch + evidence. Also includes OCR/regex document extraction into tables. Works with Ollama or vLLM, standard library only. |
| [`kaggle/gemma4_submission/`](kaggle/gemma4_submission/README.md) | Google Gemma 4 Developer Agent competition: declarative `submission/` for the `swegemma` harness, a validator/packager, and Kaggle notebook cells (start server → write submission → local eval). |

```bash
python -m pytest -q          # branch CI: 45 passed, 5 skipped; no GPU needed
python -m agentharness doctor
python -m agentharness skills .
```
