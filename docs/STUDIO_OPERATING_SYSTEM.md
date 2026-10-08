# Nessa studio orchestration and measured improvement

Nessa is a general creative-production controller. It selects workflows from the
request and installed capabilities; a specific movie premise is not required to
configure the studio. It does not promise every possible request is feasible on
the installed hardware or claim recursive self-improvement of model intelligence.

## Operational tools

| Tool | Purpose |
|---|---|
| `production_status` | Inspect brief, asset versions, task graph, stale results and trials; optional task ID shows its full contract and recent runs |
| `production_update` | Store a brief, register an asset/version, define a task or record an attributed review |
| `production_run` | Execute one ready task with bounded argv execution, required artifacts and explicit checks |
| `production_recover` | Explicitly acknowledge an interrupted run after inspecting process/artifacts; preserves the original unknown outcome |
| `production_compare` | Record matched baseline/candidate timing evidence with run IDs; never auto-promote a recipe or controller change |

State lives at `production/state.json` in the attached project. `/production` in
terminal chat shows it. Nessa reads that state when entering the project work loop.
Mutations use the existing controller, permissions, operation receipts and
checkpoints. Run approvals display the actual argv, check commands, outputs and
timeout. This remains one parent loop, not an autonomous swarm.

Asset registration fingerprints and saves a content-addressed snapshot under
`production/assets/`. Editing the source makes its registered version stale until
re-registered. Dependent work is invalidated by changed asset versions, task
contracts, dependency outputs, missing files or modified artifact hashes.
Snapshots consume disk space and currently have no automatic garbage collection.

Tasks declare `id`, `argv`, `dependencies`, `assets`, `outputs`, `checks` (arrays of
argv arrays), `timeout` and `purpose`. Register dependencies first. Cycles are
rejected atomically. There are at most 128 tasks, 32 entries per dependency/input/
output/check list, and a 1–600 second budget for each complete job and its checks.
Long renders should be explicit frame/shot chunks. Only one production job runs
at a time on this laptop. The generic managed-process tools remain available for
other long-lived applications; this is not a remote render-farm scheduler.

A zero exit status alone is insufficient: expected outputs must be nonempty and
created/refreshed by the job. Successful output-only jobs are `completed`.
`verified` means the declared technical commands also passed; it never certifies
artistic quality. If a check changes the output it was evaluating, the job fails.
Cancellation terminates the command process group, records an unknown outcome and
preserves Stop behavior. Interrupted jobs are blocked from automatic replay until
explicit recovery records explain what was inspected and why retry is safe.
Completed jobs and saved project state survive a Nessa restart. Unsaved Craft
engine documents still require explicit save/reopen as described in the creative
integration guide.

## Improvement cycle

1. Identify a real failure/bottleneck from run evidence or attributed review.
2. Freeze the input asset versions, output contract and independent check commands.
3. Register baseline and candidate recipes as separate tasks. Track check scripts
   and other input dependencies as assets, so edits invalidate old comparisons.
4. Alternate variants for at least three successful runs each. Record failures;
   comparisons reject failed runs with the same current signature.
5. `production_compare` records measured median time, sample size, executable
   fingerprints, runtime environment and run IDs. Unequal recorded environments
   or contracts cannot be compared. Fewer than three repeats are explicitly labeled.
6. Review actual visual/audio output separately. `review` notes are bound to artifact
   hashes and labeled agent-recorded; they are not impersonated human approvals.
7. Propose a recipe change with applicability, regression evidence and rollback.
   Controller code edits follow ordinary workspace, permission and verification
   rules. There is no automatic policy change or weight training.

The check contract can verify technical requirements; its completeness is still a
project responsibility. Timing does not prove equivalence of perceptual quality.
External libraries, system load and caches are not fully pinned by executable
hashes. Small trials are evidence for the tested setup, not universal rules.

## Domain workflows

The skill catalogue now covers studio orchestration, story/boards/animatics,
anime styling, modeling/rigging, acting/lip sync, simulation/FX, lighting/camera,
Blender animation, raster/photo work, vector/page/PDF work, editing, compositing,
sound, delivery and measured workflow improvement. Skill selection is task-driven;
specialist engines still advertise their real capabilities through discovery.
The system does not silently invent an installed voice generator, vision critic,
motion-capture model or cloud render fleet. Missing services can be added through
MCP/backend adapters and validated against the production contracts.

## Live validation

Artifacts: `/home/alabs/nessa-studio-pipeline-validation/`.
Two real FFmpeg presets transcoded the same registered 12-frame clip. Both used
the same registered independent checker for 320×180, 12 fps, 12 frames, one-second
duration and full decode. Three alternating runs per arm in the final comparison:
median 0.431 s baseline and 0.381 s candidate, ratio 1.13. This includes checks and
process overhead. It is an integration/timing test; perceptual quality was not
compared and no preset was promoted. Full run records, snapshots and trial IDs
are preserved in the production state.
