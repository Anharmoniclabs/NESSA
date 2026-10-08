# TripoSR image-to-3D

Nessa offers `mesh_generate` (permission-gated dev action) and `mesh_status`
(read-only). `creative_apps` reports installation; the `image-to-3d` production
skill describes source preservation, job polling, Blender import and review.
Restart existing Nessa sessions after updating Python code.

Example in a project chat: “Convert sources/character.png into a 3D model with
TripoSR, then save an editable Blender file.” The tool request is:

```json
{"image":"sources/character.png","output_dir":"models/character-v1","resolution":128}
```

Poll `mesh_status` using the returned `job` path. Do not call mesh_generate again
for an existing output directory. Jobs have independent logs, immutable input
copies and source/output SHA256 values. CPU work runs one image at a time in the
Cemí batch, with a 30-minute per-image limit. A failed/partial output is not a
successful model. GLB validation checks its container and geometry counts;
Blender readback checks decoded mesh vertices and faces separately.

## Installed runtime

`~/.local/opt/TripoSR`, Python 3.11 virtual environment, CPU PyTorch 2.2.2,
TripoSR source revision `107cefdc244c39106fa830359024f6a2f1c78871`.
Set NESSA_TRIPOSR_HOME before starting Nessa to select another installation.
The runtime uses the official public `stabilityai/TripoSR` weights downloaded
from Hugging Face. Inputs are processed locally. This does not use Higgsfield,
HF image generation billing or a substitute reconstruction model.

The official HF demo reported RUNTIME_ERROR on 2026-10-08, and the model's
inferenceProviderMapping was empty. Local CPU support is used on this machine;
GPU execution is not yet exposed by this adapter. First-time model downloads
need network access. Set up the environment using the upstream dependencies;
the runtime marker `nessa-runtime.json` records an installation whose CLI was
checked. A marker alone does not establish successful mesh generation.

## Current authorized character batch

The 40 recently downloaded PNG illustrations were copied unchanged into
`HeraldsOfTheCemi/sources/character-images/`. Their alpha masks were used to crop
transparent padding, center at 85% occupancy and composite onto gray, yielding
separate `*-prepared.png` files. No subject artwork was replaced or generated.
The separate portrait JPEG was not part of the illustrated character set.

`production/character-3d-manifest.json` tracks original Downloads paths, source
and prepared hashes, output directories and per-item status. Unknown identities
remain unassigned, using source filenames instead of invented character names.
The user-provided character bible distinguishes novel canon, design proposals
and screenplay additions; reconstructed hidden surfaces are design estimates.

Run/resume from the NESSA checkout:

```sh
python3 scripts/cemi_3d_batch.py
```

The script uses Nessa's tool dispatcher, operation receipts, production assets
and registered Blender tasks. This is deterministic automation, not a claim
that a language model directed each conversion. A lock prevents duplicate batches.
Completed and failed entries are skipped on resume. Inspect failed job logs before
any deliberate retry; retain old outputs/receipts and use a fresh versioned directory
for another reconstruction. Active jobs are polled, never automatically resubmitted.

Each successful item produces `models/triposr/<filename>/0/mesh.glb`, a named
`.blend`, and a `.validation.json` from saving and reopening that Blender file.
Original illustration files and editable geometry are retained. TripoSR estimates
one object from one view: occluded faces, backs, hands and small props may need
manual work. These meshes are unrigged and do not constitute animation-ready
characters, approved visual designs or completed animation.

References: [official TripoSR](https://github.com/VAST-AI-Research/TripoSR),
[weights](https://huggingface.co/stabilityai/TripoSR).

## Persistent batch service

The current authorized batch runs as `nessa-cemi-3d.service` in the user's systemd
manager. Inspect it with `systemctl --user status nessa-cemi-3d`; logs are available
with `journalctl --user -u nessa-cemi-3d`. The service survives this chat ending,
but is not configured to start at boot. After reboot, resume using the script above.
The gallery `production/character-3d-gallery.html` refreshes every 20 seconds and
links each completed .blend/GLB to its original art. CPU static previews are saved
beside Blender files; each .blend packs its original reference image.

## Textured display finish (user update)

The user selected textured static models, without rigging. `mesh_finish` is now
a permission-gated local job: conservative mesh cleanup, smooth subdivision
display, UV atlas, 2048px albedo baked from the supplied artwork on front-facing
surfaces, packed textures, GLB export, Blender reopen checks and three still views.
Original artwork is preserved; a separate texture preparation image extends edge
colors to avoid blank projection borders. Hidden surfaces retain TripoSR estimates.
Projection does not fix anatomical distortion or establish final artistic approval.

The batch now performs this finishing stage for every character, including earlier
base meshes. New reconstructions use marching-cubes resolution 256. Finished
packages are in models/finished-textured/<source-id>/; the gallery links them as
they complete. The prototype was visually checked from three angles; front detail
improved markedly, while side/back reconstruction quality remains limited.
46 focused regressions passed after integration of the finishing tool.

At the user's request, nessa-cemi-open-when-done.service watches the manifest and
opens the gallery plus one textured Blender model only when all 40 finish. Failures
produce a local desktop notice instead of opening an incomplete batch as done.
The watcher expires after 48 hours; neither service is configured for boot startup.
