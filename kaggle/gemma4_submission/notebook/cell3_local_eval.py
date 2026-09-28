# CELL 3: score the submission on a few public tasks with the official swegemma harness,
# using the vLLM server from CELL 1 (127.0.0.1:8000). Kaggle has no Docker, so the
# harness's subprocess sandbox is used; the real scoring run uses Docker containers.
from pathlib import Path
import json
import subprocess
import sys
import time

TASK_IDS = []        # e.g. ["requests_7205"]; empty = first task of each repo
MAX_TASKS = 4
CONCURRENCY = 2      # tasks in parallel; vLLM batches their requests
ALLOW_DOWNLOADS = False

ENV = Path("/kaggle/working/vllm_l4")
PY = ENV / "bin/python"
COMP = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WHEELHOUSE_DIR = Path("/kaggle/input/datasets/metric/gemma-4-developer-agent-wheelhouse")
SUBMISSION = Path("/kaggle/working/submission")
RESULTS = Path("/kaggle/working/eval_results") / time.strftime("%Y%m%d-%H%M%S")

assert PY.is_file(), "Run CELL 1 first (it creates the vllm_l4 environment and starts the server)."
assert (SUBMISSION / "agent.yaml").is_file(), "Run CELL 2 first (it writes the submission)."

# 1. Install the harness into the same environment, offline from the wheelhouse.
has = subprocess.run([PY, "-c", "import swegemma"], capture_output=True).returncode == 0
if not has:
    wheel = sorted(WHEELHOUSE_DIR.rglob("swegemma-*.whl"))[-1]
    links = sorted({str(p.parent) for p in WHEELHOUSE_DIR.rglob("*.whl")})
    base = [sys.executable, "-m", "pip", "--disable-pip-version-check", "--python", str(ENV),
            "install", "--no-cache-dir"] + [a for d in links for a in ("--find-links", d)]
    r = subprocess.run(base + ["--no-index", str(wheel)], capture_output=True, text=True)
    if r.returncode and ALLOW_DOWNLOADS:
        r = subprocess.run(base + [str(wheel)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("swegemma install failed:\n" + r.stdout[-3000:] + r.stderr[-3000:])
    print("Installed", wheel.name)

# 2. Pick tasks: one per repository unless TASK_IDS is set.
tasks = [json.loads(l) for l in (COMP / "tasks.jsonl").read_text().splitlines() if l.strip()]
if not TASK_IDS:
    seen = {}
    for t in tasks:
        seen.setdefault(t["repo"], t["instance_id"])
    TASK_IDS = list(seen.values())[:MAX_TASKS]
print("Tasks:", TASK_IDS)

# 3. Run the official Evaluator with the budgets from the submission's eval_config.yaml.
RESULTS.mkdir(parents=True, exist_ok=True)
runner = RESULTS / "run_eval.py"
runner.write_text(f'''
import asyncio, tempfile, yaml
from pathlib import Path
from swegemma.config import EvalConfig
from swegemma.evaluate import Evaluator
from swegemma.models import setup_gemma_model_registry

budget = (yaml.safe_load(Path({str(SUBMISSION / "eval_config.yaml")!r}).read_text()) or {{}}).get("evaluation", {{}})
comp = Path({str(COMP)!r})

async def main():
    config = EvalConfig(
        tasks_path=comp / "tasks.jsonl",
        snapshots_dir=comp / "snapshots",
        submission_dir=Path({str(SUBMISSION)!r}),
        results_dir=Path({str(RESULTS)!r}),
        graph_dir=str(comp / "graphs"),
        embeddings_dir=str(comp / "embeddings"),
        wheels_dir=comp / "wheels",
        models=setup_gemma_model_registry(),
        task_ids={TASK_IDS!r},
        sandbox="subprocess",
        max_tool_calls=budget.get("max_tool_calls", 100),
        max_time_minutes=budget.get("max_time_minutes", 60),
        timeout_seconds=budget.get("timeout_seconds", 300),
        concurrency={CONCURRENCY},
        display_mode="auto",
    )
    result = await Evaluator(config).run()
    print(f"RESOLVED {{result.resolved}}/{{result.total}}", flush=True)

# swegemma 0.2.7 caches unpacked wheels under a fixed temp name; isolate this run.
with tempfile.TemporaryDirectory(prefix="gemma-eval-") as cache:
    tempfile.tempdir = cache
    asyncio.run(main())
''')
started = time.monotonic()
proc = subprocess.Popen([str(PY), str(runner)], cwd=RESULTS, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1)
with (RESULTS / "console.log").open("w") as log:
    for line in proc.stdout:
        log.write(line)
        print(line, end="", flush=True)
proc.wait()
print(f"\nExit code {proc.returncode} after {time.monotonic() - started:.0f}s")

# 4. Per-task summary from the harness's own result files.
rows_file = RESULTS / "task_results.jsonl"
if rows_file.is_file():
    for line in rows_file.read_text().splitlines():
        r = json.loads(line)
        keys = ("instance_id", "resolved", "error", "tool_calls_used", "duration_seconds")
        print({k: r.get(k) for k in keys if k in r})
# Project the full scoring run: ~120 hidden tasks run sequentially under a 12 h cap.
durations = []
if rows_file.is_file():
    for line in rows_file.read_text().splitlines():
        r = json.loads(line)
        d = r.get("duration_seconds") or r.get("agent_elapsed_seconds") or r.get("elapsed_seconds")
        if isinstance(d, (int, float)):
            durations.append(d)
if durations:
    per_task = sum(durations) / len(durations)
    hours = per_task * 120 / 3600
    print(f"Average {per_task/60:.1f} min/task -> about {hours:.1f} h for 120 sequential tasks "
          f"({'OK' if hours < 10.5 else 'TOO SLOW: lower eval_config budgets'}; cap is 12 h incl. setup)")
else:
    print("No per-task durations found; time the run above: elapsed / tasks x 120 must stay under ~10.5 h.")
for name in ("summary.json",):
    if (RESULTS / name).is_file():
        print(name, (RESULTS / name).read_text()[:2000])
print("Traces:", RESULTS / "traces", "| Patches:", RESULTS / "patches", "| Logs:", RESULTS / "logs")
