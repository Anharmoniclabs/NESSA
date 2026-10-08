# Hugging Face image generation

## Reference editing, video and human approval

`image_edit` queues Qwen-Image-Edit-2509 with 1–3 local PNG/JPEG references.
`video_generate` queues Wan2.2-I2V-A14B. `media_job` polls and collects either
without resubmitting the paid request. Receipts and input/output hashes survive
restart. No image-to-video request is sent before the user approves the exact
input image. In the Nessa terminal, after viewing it:

```text
/approve-image art/your-image.png
```

The approval is stored outside the agent's project and bound to the image SHA256.
Changing image bytes invalidates it. There is no agent approval tool. Model-authored
review notes and `permission_mode=bypass` do not satisfy this gate. This is a
workflow control, not an OS sandbox against arbitrary shell programs running as
the same user. The Cemí autonomous image run exposes no shell or animation tool.

`studio_dataset_check` checks reviewed hashes, captions, provenance and separate
training/validation splits. It does not train a model or infer aesthetic approval.

The Cemí project is `/home/alabs/Projects/HeraldsOfTheCemi`. It includes the book,
242 page-addressable text files, source-linked shot manifests, a prepared (not
GPU-tested) AI Toolkit LoRA recipe, dataset gate and production state. RunPod,
ComfyUI and motion-reference Wan-Animate are not connected yet.

`scripts/run_pilot.py` is the initial Codex-authored deterministic tool runner.
It is not evidence of model-directed autonomy. `scripts/autonomous_director.py`
uses the real Nessa `Agent.run` model loop with a bounded image request and an
independent image delivery check; its outputs stop for explicit human approval.
The earlier pilot video was submitted before the user clarified the approval
gate and must not be treated as approved final production.

Nessa's `image_generate` defaults to `black-forest-labs/FLUX.1-dev` through the
Hugging Face router and its fal.ai provider, using the existing HF credential and
HF inference billing. `black-forest-labs/FLUX.1-schnell` is selectable for drafts.
Qwen models remain explicitly selectable; errors never trigger model substitution.
Restart an already-running Nessa session to load the updated tool catalogue.
The provider routes were checked against live HF model metadata on 2026-10-08.

Start a cloud session (the installed `nessa` launcher already enables cloud):

```sh
nessa
```

Ask for an image, specifying a destination in the attached project. Chat enters
the work workflow via `start_work`; the image tool is a permission-gated `dev`
action. It reads the credential from the configured HF client, never from model
arguments. A local-only session cannot spend cloud image credits. Chat retains
its local fallback; there is no local image model installed, so image failures
are reported explicitly rather than substituted with a different backend.

Example tool arguments:

```json
{"prompt":"Anime ocean at night beneath stars","path":"art/ocean-v1.png","width":1664,"height":928,"seed":42}
```

Each call requests one PNG (FLUX dev: 28 steps; schnell: 4; Qwen: 50), validates dimensions and
full image decoding using FFmpeg, and writes a `.generation.json` receipt with
model, provider, prompt, requested/returned seed, duration and SHA256. File
paths stay inside the project; existing outputs/receipts are never overwritten.
HF credentials go only to the HF router, not the image CDN. Response sizes and
network waits are bounded. Requests are not automatically retried: an interrupted
call may have consumed credits. Billing cost is unknown unless separately measured.

Generated images are stills. Blender/FFmpeg can animate the camera over a still;
that does not establish moving water, character acting or generated video.

## Live validation

Project on this machine: `/home/alabs/Projects/NessaOceanNight`.

- `art/ocean-night-v1.png`: 1664×928, Qwen-Image-2512/fal.ai, seed 27081998.
- Image request, download and decode: 28.85 seconds; actual HF request succeeded.
- `delivery/ocean-night-pan.mp4`: 8 seconds, 1280×720, 24 fps, 192 frames, silent.
- `render_pan.py`: repeatable FFmpeg camera recipe; `check_delivery.py`: independent
  metadata, frame count and full decoding checks.
- `production/state.json`: versioned inputs, task definition, verified execution.
- Nessa operation receipts: `~/.agentharness/runs/ocean-night-image-v1/evidence`
  and `~/.agentharness/runs/ocean-night-camera-v1/evidence`.
- Agent visually inspected source art and start/middle/end framing. This is not
  user aesthetic acceptance or a review of every frame in motion.
- 32 image, creative, production and efficient-routing tests passed.

Official references:
[HF text-to-image](https://huggingface.co/docs/inference-providers/tasks/text-to-image),
[Qwen model card](https://huggingface.co/Qwen/Qwen-Image-2512),
[HF fal.ai adapter](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/inference/_providers/fal_ai.py).

FLUX references: [HF model and providers](https://huggingface.co/black-forest-labs/FLUX.1-dev), [fal FLUX dev schema](https://fal.ai/models/fal-ai/flux/dev/api), [fal FLUX schnell schema](https://fal.ai/models/fal-ai/flux/schnell/api).

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
