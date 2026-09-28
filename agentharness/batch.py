"""Unattended batch runs over a tasks.jsonl (e.g. the Kaggle competition layout).

Each task: snapshot -> private workspace -> Agent (plans auto-approved) -> one JSON line
with the patch. Resumable: finished tasks are skipped, errored tasks are retried.
"""
from __future__ import annotations

import json
import re
import shutil
import tarfile
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .agent import Agent, AgentConfig
from .workspace import Workspace

# tasks.jsonl field names differ between datasets; the first present key wins.
ID_KEYS = ("instance_id", "task_id", "id")
PROBLEM_KEYS = ("problem_statement", "prompt", "instruction", "issue", "description", "task")
SNAPSHOT_KEYS = ("snapshot", "snapshot_path", "snapshot_dir", "repo_path")


def pick(record: dict, keys, label: str):
    for k in keys:
        if record.get(k) not in (None, ""):
            return record[k]
    raise KeyError(f"No {label} field among {keys}; record keys: {sorted(record)}")


def find_snapshot(comp: Path, record: dict, task_id: str) -> Path:
    snaps = comp / "snapshots"
    for k in SNAPSHOT_KEYS:
        if record.get(k):
            p = Path(record[k])
            for cand in (p, comp / p, snaps / p, snaps / p.name):
                if cand.exists():
                    return cand
    matches = sorted(snaps.glob(f"{task_id}*"))
    if not matches:
        raise FileNotFoundError(f"No snapshot for {task_id} under {snaps}")
    return matches[0]


def materialize(snapshot: Path, dest: Path) -> Path:
    """Return a directory with the snapshot's files (extracting archives into dest)."""
    if snapshot.is_dir():
        return snapshot
    if dest.exists():
        shutil.rmtree(dest)
    if zipfile.is_zipfile(snapshot):
        with zipfile.ZipFile(snapshot) as z:
            for name in z.namelist():  # refuse archive path traversal
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError(f"Unsafe path in archive: {name}")
            z.extractall(dest)
    elif tarfile.is_tarfile(snapshot):
        with tarfile.open(snapshot) as t:
            t.extractall(dest, filter="data")
    else:
        raise ValueError(f"Unsupported snapshot format: {snapshot}")
    kids = list(dest.iterdir())
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else dest


def safe_name(task_id: str) -> str:
    return re.sub(r"[^\w.-]", "_", task_id)[:120]


def solve_one(client, comp: Path, record: dict, runs: Path, config: AgentConfig) -> dict:
    task_id = str(pick(record, ID_KEYS, "id"))
    problem = pick(record, PROBLEM_KEYS, "problem")
    work = runs / safe_name(task_id)
    source = materialize(find_snapshot(comp, record, task_id), work / "source")
    ws = Workspace.create(source, work)
    result = Agent(client, ws, config=config).run(problem if isinstance(problem, str) else json.dumps(problem))
    return {"task_id": task_id, "patch": result.patch, "status": result.status,
            "steps": result.steps, "seconds": result.seconds, "checks": result.checks}


def run_batch(client, comp: Path, out: Path, runs: Path, *, workers: int = 4,
              config: AgentConfig | None = None, limit: int | None = None,
              id_key: str = "instance_id", patch_key: str = "model_patch") -> Path:
    comp, out, runs = Path(comp), Path(out), Path(runs)
    config = replace(config or AgentConfig(), require_approval=False)
    rows = [json.loads(l) for l in (comp / "tasks.jsonl").read_text().splitlines() if l.strip()]
    print(f"{len(rows)} tasks; first record keys: {sorted(rows[0]) if rows else []}")
    rows = rows[:limit] if limit else rows
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if r.get("status") != "error":
                done.add(r[id_key])
    todo = [r for r in rows if str(pick(r, ID_KEYS, "id")) not in done]
    print(f"{len(done)} already done, {len(todo)} to run with {workers} workers.")
    runs.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock, started = threading.Lock(), time.monotonic()
    with out.open("a") as f, ThreadPoolExecutor(max(1, workers)) as pool:
        futures = {pool.submit(solve_one, client, comp, r, runs, config): r for r in todo}
        for fut in as_completed(futures):
            tid = str(pick(futures[fut], ID_KEYS, "id"))
            try:
                res = fut.result()
            except Exception:
                print(f"[{tid}] ERROR\n{traceback.format_exc()[-1500:]}")
                res = {"task_id": tid, "patch": "", "status": "error", "steps": 0}
            row = {id_key: tid, patch_key: res["patch"],
                   **{k: v for k, v in res.items() if k not in ("task_id", "patch")}}
            with lock:
                f.write(json.dumps(row) + "\n")
                f.flush()
            print(f"[{tid}] {res['status']} in {res['steps']} steps, patch {len(res['patch'])} chars "
                  f"| {time.monotonic() - started:.0f}s", flush=True)
    return out
