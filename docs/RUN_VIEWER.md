# Local run evidence viewer

The viewer displays existing local run artifacts. It has no model client, run button,
approval action, shell tool, editing endpoint, or patch-application endpoint.

## Start

From the repository checkout, with Python 3.10+ on Linux or macOS:

```bash
python -m agentharness.viewer --runs ~/.agentharness/runs --port 8765
```

Open the exact URL printed by the command, normally `http://127.0.0.1:8765/`.
Use `--port 0` to choose a free port. Stop with Ctrl-C. No dependencies need installing.
Do not use `localhost`, a LAN address, a proxy, or public hosting: the server accepts
only its exact loopback IP and port. Windows is currently rejected at startup because
the safe reader requires POSIX descriptor-relative opens and `O_NOFOLLOW`.

For the current experiment layout:

```bash
python -m agentharness.viewer --runs "$HOME/nessa-three-stage-runs" --port 8765
```

For durable runs, use `--runs "$HOME/nessa-durable-runs"`. The viewer reads
exported evidence, not `session/` state generations. A terminal durable state
without its final result/end-event exports remains `in_progress_or_interrupted`
in the viewer; explicit terminal resume can return the saved result without
changing historical exports. Durable cancellation also changes only session state,
so this viewer may retain its last incomplete evidence label for a cancelled run.
Some durable solve/checkpoint boundaries have no corresponding display event;
stage and changed-file panels can remain incomplete until terminal exports.
Use the durable CLI/state contract, not the viewer, to decide whether a run can resume.

Point it at the narrowest relevant run collection. The root itself can be one run,
a normal harness runs folder, or a bounded parent containing nested exports.

## What the interface means

- **Controller status** comes from `result.json` or the controller's terminal `end`
  event. A process exit code, model statement, proposed plan, or passing baseline
  check cannot mark a run verified.
- **Independent grade and replay** are separate audit evidence. Controller
  `verified` does not imply an independent grade or successful patch replay. Missing
  audits stay unavailable. Failed checks and failed graders remain visible.
- **Stages** mean stage-entry events were recorded, not that every stage passed.
  A missing final result is labeled `in_progress_or_interrupted`; the viewer does
  not infer whether a process is alive from an old event or modification time.
- **Approval** requires a boolean approval event. A recorded plan without an
  approval event is labeled approval unavailable. Planned scope and actual changed
  files are separate panels.
- **Changes and patch** prefer final artifacts. If only a checkpoint is available,
  both the file list and patch are explicitly labeled checkpoint, not final. Empty
  patches differ from missing patches.
- **Measurements** are shown only when recorded. Run, active, approval, and process
  wall times retain their separate meanings. Token totals come from an audit, not
  an estimate or a partial sum of model events. Missing numbers are not zero.
- **Model narration** is advisory, isolated from authoritative status. Legacy model
  summaries and raw model messages are not displayed.

The run list refreshes every ten seconds while the page is visible. Clicking a run
turns off “Follow most recently updated,” so refresh will not change that selection.
Search is local to the returned run list. Temporary read errors retain the last
visible evidence and show a stale/error message. The viewer does not modify run data.

## Supported files and layouts

A run can contain `evidence/`, `work/evidence/`, or the evidence files directly.
Legacy, three-stage, code-only/native, and multi-file experiment formats use the
same bounded projection:

- Evidence: `result.json`, `events.jsonl`, `patch.diff`
- Incomplete-run fallback: `checkpoints/edit-NNNN.diff` (4–8 digits)
- Run metadata: selected fields of `job.json`, `job-result.json`, `cancelled.json`
- Audits: selected fields of `audit.json` or `multifile-audit.json`, including
  independent/replay grades, integrity booleans, and token measurements
- Resources: only the peak resident-memory measurement from `resource-summary.json`

It does not read `messages.json`, HTTP transcripts, console logs, live logs,
resource samples, environment/config dumps, model weights, or arbitrary source
files. `evidence_dir` strings inside artifacts are never followed. Unknown fields
are ignored; malformed, partial, unsafe, or oversized files produce warnings.

Discovery is limited to 500 runs, 4,000 directories, and depth six. Each input file
is capped at 2 MiB, events at 20,000 lines, and the displayed patch at 100,000
characters. A discovery-limit warning asks for a narrower root. Large files are
unavailable rather than partially parsed as complete results.

## Local security boundary

The root directory is pinned by file descriptor. Every directory and file read
is descriptor-relative with no symlink following. Symlink components, traversal,
hard-linked files, and special files are refused, including when a directory is
replaced during a request. Reads cannot escape through paths recorded in evidence.
Opaque run IDs resolve only through the server's discovered index; there is no
path/file query parameter or arbitrary file-serving route.

Only `127.0.0.1` is bound. Host, Origin, and Fetch Metadata guards reject foreign
browser origins. Responses have no CORS grant, no caching, a restrictive CSP,
`nosniff`, no referrer, and no framing. UI evidence is inserted with `textContent`,
never HTML. Assets are bundled; there are no external scripts, fonts, or analytics.

Raw model fields are excluded. Text containing known private-reasoning markup is
omitted rather than attempting to expose a safe suffix. Recognizable credential
values are redacted, and sensitive patch filenames/private-key blocks are omitted.
These are defense-in-depth heuristics, not a general secret detector. Only point the
viewer at local run collections you intend to inspect, and do not expose the viewer
publicly or treat it as a secure sharing/export service. A process already running
as the same OS user can read that user's local files; this is not OS-user isolation.

## Verification

```bash
python -m unittest agentharness.tests.test_viewer -v
python -m unittest discover -s agentharness/tests -v
python -m compileall -q agentharness
node --check agentharness/viewer_static/app.js  # optional syntax check if Node exists
```

Tests create isolated synthetic artifacts. They cover controller/advisory separation,
missing and malformed data, all layouts, checkpoint labeling, audit separation,
zero vs missing metrics, discovery limits, symlinks/hardlinks/FIFOs/traversal,
directory replacement, sensitive text, loopback/Host/Origin restrictions, read-only
HTTP methods, fixed assets, and transcript exclusion. No test calls a model.

Composition tests in `agentharness.tests.test_durable_viewer` exercise actual
approved pause/resume artifacts, terminal no-op behavior and an interruption
between terminal-state commit and convenience exports. Browser rendering and
interactive visual inspection are not covered by these filesystem/HTTP tests.
