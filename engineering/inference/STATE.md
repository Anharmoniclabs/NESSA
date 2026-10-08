# Current working state

Updated: 2026-10-08.

## Completed

- Live three-task pilot: 18 attempts with raw and Nessa arms. Portable summary in
  baseline.json; full evidence in `/home/alabs/nessa-cloud-pilot-2026-10-08/`.
- Overhead investigation: model round trips and repeated prompt context dominate;
  about 98.4% of Nessa run time was spent waiting for API responses.
- Persistent context workspace created here and linked from the root AGENTS.md.
- Prior cloud fixes: specific-model selection retains local fallback; doctor fails
  when every cloud model is unavailable. Changes remain uncommitted with the pilot.
- Desktop palette updated to warm light surfaces, dark text and a single teal accent.
  Header and composer were reorganized; the default window now fits the laptop display.
- Native preview inspected at 1180x688 and populated chat at 820x640. Composer and
  permission actions were checked at the minimum size. All 29 desktop/UI tests passed
  on the real display, including five GUI tests; none skipped.

## Earlier checkpoint (superseded by inference experiments)

- Request accounting and one controlled change to planning/tool-call behavior.
- The context workspace and interface update are implemented; inference optimization
  remains a proposal with no claimed speed or token improvement.

## Decisions

- Preserve the original benchmark as baseline evidence.
- Treat overhead fixes as controlled experiments; no benchmark gain has been claimed.
- Keep model/provider availability separate from coding correctness.
- Use this repository workspace for specifications and context; no external Space
  or cloud compute environment has been provisioned.

## Earlier next step (completed below)

Implement request-accounting gaps first, then test one change to planning/tool-call
behavior. Compare to the baseline on fresh runs; add a basic-agent arm and larger
tasks before choosing an inference policy for production.

## Inference instrumentation and experiments

See EXPERIMENTS.md for live cache, prefill/decode and concurrency measurements.
Implemented request metrics, production cloud streaming without a UI callback,
prompt/compaction byte accounting and isolated batch clients. Existing local
parallelism, KV format and compaction limits remain pending controlled comparisons.
Speculative decoding and disaggregation require a compatible controllable server;
no deployment or speedup for those mechanisms is claimed.

Validation complete: focused regressions passed; live native-tool stream and one
full streamed coding task passed independent checks. Cloud prefix hits and local
prefill reuse are measured. Additional nonstreaming coding probe passed 2/3,
with one provider timeout/fallback failure. See EXPERIMENTS.md for sample sizes,
paths and limits. Next: matched long-context cache/compaction quality trials;
backend-controlled speculation/KV/disaggregation need a compatible test server.
# Cloud image capability — 2026-10-08

Added permission-gated `image_generate`, Qwen-Image-2512 through HF/fal.ai.
The configured HF client grants cloud opt-in; local-only sessions cannot call it.
No local image fallback is configured; chat fallback is unchanged. One live image
request/download/decode took 28.85 seconds, with model/provider/seed/hash receipt.
This is image request wall time, not text TTFT or decode throughput. Billing unknown.
An eight-second FFmpeg camera animatic passed independent decoding/frame checks.
32 focused regression tests passed. See `docs/CLOUD_IMAGES.md` for artifacts,
limits and official API sources. Next unresolved capability: actual image-to-video
subject/water motion; the delivered shot animates only the camera over a still.

## Cemi director and media approval — 2026-10-08

Added HF queue-backed Qwen image editing and Wan I2V tools, resumable receipts,
input/output hashes, and a dataset readiness tool. Live requests returned actual
PNG/MP4 artifacts. The first directed pilot was Codex-authored, not model-selected.
After user correction, Wan submission requires explicit human approval tied to
the exact source image SHA256; model review notes and bypass do not satisfy it.
48 focused media/creative/production/terminal/routing tests passed.

A real Nessa Agent.run director initially stalled: GLM-5.3 repeatedly exhausted
4096 completion tokens and produced incomplete tool calls. A second image-only
run with 8192 tokens and reasoning_effort=low completed in 7 model turns, chose
the page-13 coin insert, wrote its own prompt, generated a Qwen image, registered
it and passed independent PNG checks. Both settings changed; this is recovery
evidence, not an isolated inference benchmark. No animation was offered or sent
in that successful run. Explicit image approval remains pending. Evidence:
`~/.agentharness/runs/cemi-autonomous-1791474713411736613/evidence`; concise proof:
`/home/alabs/Projects/HeraldsOfTheCemi/shots/autonomy-proof.json`.

RunPod/ComfyUI, Wan-Animate motion transfer and LoRA training remain unconfigured.
The supplied pack has one composite moodboard and zero approved training images.
The book is imported as 242 searchable source pages.

## FLUX through Hugging Face — 2026-10-08

User requested HF FLUX instead of Qwen/Higgsfield. `image_generate` now defaults
 to black-forest-labs/FLUX.1-dev (28 steps); FLUX.1-schnell (4 steps) is selectable.
Existing HF opt-in, permissions, no automatic retries, fresh output checks,
credential isolation, and PNG decoding checks are retained. The Cemí full-book
director and project instructions now select FLUX for new stills. Existing sessions
must restart to load Python changes. Qwen editing and Wan video are unchanged.

Validation: 36 image/media/creative/production tests passed; syntax and diff checks
passed. One real Nessa tool execution generated a 1664x928 FLUX-dev PNG through
router.huggingface.co/fal-ai/fal-ai/flux/dev in 8.82 seconds, including download and
full decoding. This was a deterministic tool invocation using the existing shot
prompt, not a new autonomous GLM direction run. Billing amount unknown. Artifact:
`/home/alabs/Projects/HeraldsOfTheCemi/art/ch01-jerome-0307-flux-v1.png`.
Receipt contains prompt, model, seed and hash. Operation evidence:
`/home/alabs/.agentharness/runs/cemi-flux-1791480595103395564/evidence`.
Live schnell was not tested. User aesthetic approval remains pending; no animation
was submitted. Next: user review of the FLUX still.

## TripoSR integration and character batch — 2026-10-08

Added permission-gated mesh_generate, read-only mesh_status, creative inventory,
chat/tool routing and image-to-3d production skill. Backend is local official
TripoSR on an isolated Python 3.11/CPU Torch runtime, public HF model weights.
Official HF demo was in RUNTIME_ERROR and no inference provider was listed.
Existing image-generation routes are unchanged. Durable workers preserve source
hashes, separate outputs and explicit failures. No retries of existing jobs.

User authorized all 40 recent Downloads character illustrations to 3D and Blender.
Prepared copies retain original artwork; transparent padding is cropped and gray
background composited separately. The supplied character bible is recorded as
user-provided continuity guidance; filename identities remain unassigned.
First local reconstruction completed in 85.2 seconds, 5,454 vertices/10,884 faces.
Blender reopened the saved file; static preview exposed a glTF Z-up conversion
issue, corrected in the shared import recipe. Original image is packed into .blend.
First corrected preview was visually inspected: rough geometry and soft facial
detail, not animation-ready. No animation or rigging was performed.

54 focused tests passed. Batch is running via persistent user service
nessa-cemi-3d.service with per-image tool receipts and production asset tracking;
2/40 completed at this checkpoint. This is deterministic Nessa tool automation,
not a claim of autonomous GLM creative direction. Full-batch completion pending.
Progress: /home/alabs/Projects/HeraldsOfTheCemi/production/character-3d-manifest.json
Gallery: /home/alabs/Projects/HeraldsOfTheCemi/production/character-3d-gallery.html
See docs/TRIPOSR.md.

## Textured display finishing — 2026-10-08

User rejected rough previews and explicitly chose textured finished display models,
without rigging. Added mesh_finish as a permission-gated durable local job; includes
original-art front projection, 2K atlas, packed Blender file, textured GLB and three
review views. First prototype passed Blender readback, has zero nonmanifold edges,
and was visually inspected. Front art is clearer; inferred side/back geometry is
not artistically final. No claim of final visual acceptance. 46 regressions passed.
The existing batch was stopped, completed assets preserved, interrupted work
recorded, and the new finishing pipeline started as a persistent user service.
User requested opening when done; a separate service waits for all 40 finish_status
values to become complete, then opens the gallery and first textured .blend.
Remaining: full batch completion and geometric/visual review of all characters.
