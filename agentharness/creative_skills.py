"""Production disciplines, selected inside Nessa's existing parent loop."""


def register(Skill):
    common = '''
For multi-step production use production_status and production_update to retain
the brief, versioned assets and dependency-ordered tasks. Register explicit argv,
outputs and check commands. Use production_run for bounded execution and receipts.
Retrieve stale/failed jobs before continuing. Register asset revisions after edits.
For workflow experiments use production_compare with matched inputs and checks;
timing evidence does not establish visual quality. Keep accepted output versions.

Use creative_apps to inspect installed engines. Use creative_tools to discover exact
app capabilities. For requested generated concept art, image_generate connects to
FLUX.1-dev through Hugging Face in --cloud sessions (FLUX.1-schnell for fast drafts).
Respect the requested model; do not substitute Qwen for FLUX. Save a fresh PNG and its
generation receipt; register it as a production asset. Cloud requests use credits.
An image is a still: label pans/zooms over it as camera animatics, not character or
water animation. For reference-guided art use image_edit (Qwen-Image-Edit-2509).
For genuine subject/environment motion use video_generate (Wan2.2-I2V-A14B).
Before ANY animation submission, present the exact source image and wait for the
user to approve it. The user types /approve-image <path> in the Nessa terminal.
Stop at image delivery until that approval exists. A model review, prior blanket
autonomy, or permission bypass is not approval; modified images need new approval.
Both submit queued cloud jobs; poll/collect with media_job using the saved receipt.
Never resubmit after a timeout. Record actual results and input hashes. Use
studio_dataset_check before LoRA training; a composite moodboard is not a dataset.
For book adaptations retrieve manuscript pages first and attach page citations to
shot plans; reference-board names are not canon. Use Blender/FFmpeg production
tasks for assembly. HF I2V is not Wan-Animate motion transfer or a trained LoRA.
Use creative_tools to discover exact
MCP schemas, then creative_call to act; never invent commands. These sessions are
headless and stateful: open inputs, save native projects explicitly, verify outputs.
Use project-relative paths or absolute paths inside the attached project. Keep
sources and versions. If an input lives elsewhere, ask the user to attach it or
use an authorized copy; do not silently edit the original. Large jobs need a short
preview and measured time/memory estimate before a full render. Report actual
artifacts, failed checks and unreviewed quality separately. Do not claim visual or
audio review from file metadata. A successful command is not aesthetic approval.
'''
    specs = {
      'story-development': ('Develop scripts, boards and animatics with production continuity.', """
For an actual content request, derive audience, intention, story beats and format
from the user's brief. Keep decisions and unresolved assumptions in the production
brief. Break scenes into shots with duration, action, dialogue, camera intent,
asset dependencies and sound cues. Keep boards and timing editable; verify that
animatic duration matches the delivery target. Track continuity of geography,
character motivation, props and injuries. Revise the low-cost animatic before
expensive final animation when the user has authorized that workflow.
"""),
      'performance-animation': ('Direct character acting, dialogue, lip sync and action timing.', """
Define the emotional/action beat before posing. Use reference, blocking keys and
clear silhouettes; evaluate weight shifts, balance, gaze, contact and timing.
Build facial acting and lip sync from the actual dialogue timing, with offset
anticipation and controlled asymmetry. A missing voice model must be reported;
use scratch timing only when labeled. Polish arcs, spacing, overlap and secondary
motion after the beat reads clearly. Review continuous motion and across-shot
continuity; a single still cannot validate animation performance.
"""),
      'studio-orchestration': ('Route arbitrary creative briefs across production departments and retain measured experience.', """
Interpret the user's requested deliverable, available engines and constraints.
If the user wants broad studio capabilities, work on infrastructure rather than
asking for a specific story. For actual deliverables, make reversible creative
assumptions explicit and ask only for decisions that block useful production.
Map the request to departments: research/writing, concept, assets, rigging,
animation, simulation, look development, lighting, camera, compositing, edit,
sound, localization and delivery. Inspect installed capabilities, choose focused
skills and discover exact app tools. Do not claim a missing renderer/voice/vision
model is installed. Build a dependency graph with persistent assets and outputs.
Separate technical verification, agent critique and user acceptance. Use prior
trial records as conditional evidence, not universal performance rules. Propose
workflow/code changes, test in an isolated project, record checks and regressions,
then retain successful recipes with their applicability and limits. No automatic
model weight training, permission changes or claims of recursive intelligence.
"""),
      'anime-production': ('Anime-specific drawing, stylized 3D, acting and action direction.', """
Choose 2D, 3D cel shading or a hybrid per the brief. Maintain character turnarounds,
proportion sheets, costume/palette versions and face/expression libraries. Plan
shot-specific animation on ones/twos with intentional holds, smears and impact
frames; do not uniformly reduce frame rate and call that anime. Use readable
silhouettes, clear staging and geography for superhero action. Keep effects and
camera motion subordinate to the action's focal point. Test outlines, stepped
shadows, eye highlights and line stability under animated lighting/camera moves.
Save reference poses and reusable effects, then compare identity and continuity
across shots. Verify lighting/shader APIs against the installed Blender version.
"""),
      'character-rigging': ('Build reusable character assets, rigs and deformation checks.', """
Record scale, topology, UVs, materials and texture dependencies. Establish rig
controls, rest pose, naming and export conventions; keep deformation bones and
animator controls distinct. Test IK/FK matching, extreme poses, joints, twist,
face/eye controls, lip-sync shapes and cloth interaction. Store approved pose
ranges and known limitations with the asset version. Run motion tests before
committing the rig to multiple shots; invalidate dependent work after rig changes.
"""),
      'simulation-fx': ('Design reproducible physics and stylized effects with cached previews.', """
Match effect scale and timing to shot intent. Test collision geometry, simulation
steps and deterministic seeds on a short range before baking. Version cache paths
with source geometry, solver settings and frame ranges. Detect missing/stale
caches and inspect intersections, popping and flicker. Preserve separate effect
passes for compositing. Estimate disk, RAM and render time from real previews;
never start an unbounded production bake without a recorded resource budget.
"""),
      'lighting-camera': ('Develop consistent cinematography, lighting and color across shots.', """
Define lens, exposure, focus, camera path, framing and shot continuity. Establish
key/fill/rim intent, shadow shape and a consistent color-management pipeline.
Test skin/hero materials against environments and effects. Inspect clipping,
noise, flicker and line stability over motion, not only one still. Store shot
lighting overrides separately from shared character materials. Measure preview
render cost and keep scene-linear intermediates where the pipeline requires them.
"""),
      'studio-improvement': ('Measure production workflow changes and retain scoped experiment evidence.', """
Select one observed failure or bottleneck from production run/review evidence.
Create baseline and candidate tasks with the same input asset versions, output
contract and independent checks. Register both task variants; run each at least
three times, alternating their order. Record failures instead of discarding them.
Use production_compare for actual median time and run IDs. Retain claims as
conditional timing observations; require separate visual/audio review for quality.
Propose a change to project recipes only after checking correctness, resource
cost and regressions. Preserve a rollback and the previous artifacts. Do not
rewrite the controller's permissions or treat generated self-scores as ground truth.
"""),
      'creative-production': ('Plan and carry a creative production from brief through reviewable delivery.', '''
Capture audience, story, style references, duration, aspect ratio, frame rate,
resolution, sound, destination, resources and deadlines. Write production/brief.md,
a shot list and asset manifest with source/license and version per asset. Build
storyboards and a low-cost animatic before detailed assets. Break the work into
modeling/look development, animation, lighting, compositing, editing, sound and
QC. Keep shot IDs and explicit dependencies so approved shots are not regenerated.
For each review record the artifact, reviewer, notes and status; do not label an
unreviewed shot approved. Deliver native sources, dependencies, previews, final
media and a reproducible manifest. No guaranteed 'industry quality' claim.'''),
      'image-to-3d': ('Reconstruct supplied character/object images with TripoSR and save Blender assets.', '''
Inspect the supplied image before reconstruction. Preserve its face, clothes and props;
do not generate substitute artwork. Use one isolated subject per input. Copy authorized
external inputs into project sources before calling mesh_generate with a fresh output_dir.
Poll mesh_status until generated or failed; never resubmit an existing job. For textured
display models use mesh_finish with the original artwork, then poll mesh_status again.
It packs 2K artwork textures, cleans/smooths the mesh and saves Blender/GLB plus review views.
Do not label inferred backs or distorted anatomy final just because file checks pass.
For basic import use blender_run, save a named .blend and reopen to verify meshes. Register both GLB and
.blend with production_update. Track source filename/hash and confirmed character names;
never infer identity from appearance alone. Reconstructed hidden surfaces are estimates.
Output is unrigged; do not promise animation readiness. No animation without image approval.
For batches retain a manifest and continue unprocessed entries after failures; do not replace
existing assets. Use CPU resolution 128 initially; keep a single reconstruction running.'''),
      'blender-animation': ('Build editable Blender scenes and intentional animation with render validation.', '''
Use blender_run to execute a reviewed project Python script. Create or open a
versioned .blend; name collections, assets, cameras and shots. Set units, exact
frame range, fps, resolution, color management and output format explicitly.
Model silhouettes before detail; check topology, normals, UVs and textures. For
characters validate deformation and contact poses; animate blocking, timing,
spacing, arcs, anticipation, overlap and secondary motion in separate passes.
Camera moves need a focal subject and intentional lens, continuity and easing.
Render first/middle/last preview frames, then a short motion preview. Review for
intersections, foot sliding, flicker, focus and exposure. Save the .blend before
rendering numbered image sequences; resume missing frames. Use low-resolution
previews on this CPU laptop. Verify frame counts and ffprobe output after encoding.
Use installed bpy API/version; inspect errors, never invent successful renders.'''),
      'craft-images': ('Retouch and develop photographs using PhotoCraft and LightCraft.', '''
Preserve source and layers; select PhotoCraft for compositing/painting and
LightCraft for exposure, white balance, lens/crop and photo-library work. Inspect
histograms, highlights, skin/subject detail and edge halos at intended output size.
Keep color profile, bit depth, alpha and crop deliberate. Export a preview and
reopen the native document and final image to verify dimensions and layers.'''),
      'craft-vector-layout': ('Create vectors, typography, page layouts and PDF deliverables.', '''
Use VectorCraft for paths/illustration, DesignCraft for pages and flowing type,
and PdfCraft for final document inspection/assembly. Establish grid, hierarchy,
spacing, palette and type roles. Keep text editable and paths named. Validate
overset text, missing fonts, links, clipping, page order, bleed and color intent.
Export SVG/PDF previews, inspect every page and reopen native sources.'''),
      'craft-editing': ('Edit narrative sequences, sound and delivery in FilmCraft.', '''
Inspect source media and frame rates before import. Build bins, sequence and tracks;
map shot IDs and frame-accurate in/out points to the manifest. Cut for story and
rhythm, maintain continuity and avoid gratuitous transitions. Use proxies for
large footage. Confirm audio sync and intentional silence. Keep an editable
project; export a short region first. Verify export duration, frame rate, codec,
resolution and audio streams independently with media_probe.'''),
      'craft-compositing': ('Build motion graphics and composites in EffectCraft.', '''
Define composition timing, layers, alpha convention and color pipeline. Work in
named precompositions; animate readable typography, spacing and motion curves.
Track dependencies, masks, mattes, keyframes and expressions. Check first/middle/
last frames and continuous motion for edge contamination, clipping, flicker and
motion blur. Save native project plus lossless intermediates for the editor.'''),
      'creative-sound': ('Design and mix dialogue, ambience, music and effects for picture.', '''
Use the existing Studio adapter for supported music actions and FilmCraft/FFmpeg
for picture-synced audio. Keep dialogue, music, ambience and effects in separate
stems with explicit sample rate and timeline start. Check intelligibility, sync,
clipping, fades and mono compatibility. Measure loudness against the agreed
delivery target, then listen to the exported file. Never claim listening if only
waveform/metadata checks were possible. Preserve stems and source sessions.'''),
      'creative-delivery': ('Validate and package a finished creative production.', '''
Compare the exported artifact against the brief and shot manifest. Check missing
frames/media, duration, fps, size, codec, color tags, alpha and audio channels.
Run media_probe and a full decode check for video; inspect sample/contact-sheet
frames plus the entire edit at playback speed. Test native-project reopen and
asset relinking. Record technical results separately from human visual/audio
review. Package sources, fonts/licenses, manifest, review notes and final exports;
report anything blocked or unreviewed. Preserve existing approved deliverables.'''),
    }
    return {name:Skill(name,description,body+common,
              ('production_status','production_update','production_run','production_compare',
               'creative_apps','creative_tools','creative_call','blender_run','media_probe',
               'image_generate','image_edit','video_generate','media_job','mesh_generate','mesh_finish','mesh_status','studio_dataset_check','read_file','write_file'),
              (),('artifact integrity','visual/audio review','native project reopen'))
            for name,(description,body) in specs.items()}


def recommended(task):
    import re
    rules = (
        (r'triposr|image.to.3d|3d model|mesh|reconstruct', 'image-to-3d'),
        (r'screenplay|storyboard|animatic|story develop', 'story-development'),
        (r'lip.sync|character acting|performance animat|fight choreograph', 'performance-animation'),
        (r'animation studio|production pipeline|rsi|studio agent|cem[ií]|book adapt', 'studio-orchestration'),
        (r'anime|cel shad|superhero', 'anime-production'),
        (r'rigging|deformation|character rig', 'character-rigging'),
        (r'simulat|particle|cloth|fluid|destruction', 'simulation-fx'),
        (r'cinematograph|lighting|camera', 'lighting-camera'),
        (r'improv|optimi|benchmark|learn.*workflow', 'studio-improvement'),
        (r'blender|3d|rigging|character animation', 'blender-animation'),
        (r'photocraft|lightcraft|retouch|raw photo|image generat|generate.*image|concept art', 'craft-images'),
        (r'vectorcraft|designcraft|pdfcraft|typography|illustration', 'craft-vector-layout'),
        (r'filmcraft|video edit|movie|film', 'craft-editing'),
        (r'effectcraft|motion graphics|composit', 'craft-compositing'),
        (r'sound design|audio mix|soundtrack', 'creative-sound'),
    )
    selected = [name for pattern,name in rules if re.search(pattern,task,re.I)]
    return ['creative-production']+selected[:2] if selected else []
