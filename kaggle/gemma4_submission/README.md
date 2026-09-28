# Gemma 4 Developer Agent — submission + notebook

The competition does **not** run your Python. You submit `submission.zip`: a declarative
ADK agent config (`agent.yaml`, prompts, sampling, budgets). Kaggle's `swegemma` harness
runs the agent loop with its 9 sandboxed tools, then grades the resulting patch with
hidden tests in a fresh container. Score = fraction of tasks resolved.

```text
submission/
├── agent.yaml            flat root agent, all 9 harness tools, the single allowed model
├── prompts/system.md     the workflow: locate → reproduce in /tmp → minimal fix → targeted test → clean → submit_patch
├── configs/sampling.yaml temperature 0.2, 8192 output tokens, 3072 thinking budget
└── eval_config.yaml      per-task budget: 25 min, 60 tool calls, 120 turns, 240 s per command
```

## Notebook (4× L4, inputs attached)

| Input | Path |
|---|---|
| Model | `/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2` |
| Wheelhouse | `/kaggle/input/datasets/metric/gemma-4-developer-agent-wheelhouse` |
| Competition | `/kaggle/input/competitions/gemma-4-developer-agent` |

1. **`notebook/cell1_start_server.py`**: starts vLLM with the scoring settings (0.80 GPU
   memory, 32k context, gemma4 parsers, thinking enabled) and checks that the server
   returns a structured tool call. Only the wheelhouse is used for installs. The
   competition's `wheels/` folder holds old repository dependencies and is kept out.
2. **`notebook/cell2_write_submission.py`**: writes `submission/`, validates it and
   packs `/kaggle/working/submission.zip`.
3. **`notebook/cell3_local_eval.py`**: installs `swegemma` from the wheelhouse and runs
   the official `Evaluator` on one task per repository, using your submission's budgets.
   It prints resolved counts and where the traces, patches and logs are.

Kaggle notebooks have no Docker, so step 3 uses the harness's subprocess sandbox.
Scoring uses Docker containers (4 GB RAM, 2 vCPUs, offline). Local numbers are a
guide, not the leaderboard.

## Editing loop

```bash
# edit submission/..., then:
python validate_submission.py submission          # offline pre-flight check
python build_cells.py                             # refresh notebook/cell2 with your edits
python -m pytest -q test_gemma4_submission.py
```

`validate_submission.py` catches problems before a Kaggle run: a wrong or mixed model,
unknown tools, disallowed files, `!include` paths that don't resolve, generation limits,
the `evaluation:` key, and missing adapter weights. It also catches `{placeholders}` in
prompts. ADK fills those from session state at runtime, and only
`{problem_description}` and `{hints?}` exist.

## What to tune next, based on traces (`eval_results/*/traces/`)

- The model loops on reads without editing → tighten step 2 of the prompt.
- It submits without testing → add a check requirement before `submit_patch`.
- It times out → lower `max_time_minutes` / `max_tool_calls`, or shorten thinking.
- Budgets vs runtime: check the competition's notebook runtime limit on the rules page
  before raising `eval_config.yaml`. About 120 hidden tasks run in the scoring job.

Sources: the harness behaviour here follows `HARNESS_README.md` from the competition
dataset (copy consulted: github.com/rishaviitd/kaggle.gemma.coding.agent,
`context/harness.md`). Re-check it against the `HARNESS_README.md` in your attached
competition input.
