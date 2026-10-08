# Terminal chat, project graph and creative production

## Start

Run `nessa` in a project directory. From your home directory, Nessa attaches the
small `~/.agentharness/chat` workspace. The installed launcher uses cloud models,
local fallback and per-action permissions. Repository CLI defaults remain local
and private-copy unless explicitly configured otherwise.

The terminal interface preserves scrollback and uses your terminal background,
with restrained cyan accents. It streams replies, displays tool/check activity,
provides readline history and Tab completion, and shows permission requests.
It is a scrollback interface, not a full-screen Codex clone. Use
`nessa chat . --plain` for plain output; redirected input/output disables live UI.
`NO_COLOR` disables color. Ctrl-C during work preserves the session; at the input
prompt it exits. `/quit` exits. Session evidence is shown by `/status`.

| Command | Purpose |
|---|---|
| `/help` | Available commands |
| `/apps` | Installed creative engines and executable paths |
| `/skills` | Built-in and repository production workflows |
| `/graph` | Index stats for the attached workspace |
| `/graph question` | Retrieve cited project evidence and relationships |
| `/status` | Serving model, session path, request usage |
| `/diff` | Current text patch |
| `/paste` | Multiline input, terminated by `/send` |
| `/clear` | Redraw the display, retaining the conversation |
| `/apply` | Existing private-workspace patch application |

## Graph RAG

`agentharness/graph.py` indexes source files, Python symbols, imports, statically
named calls and local Markdown links. Queries rank lexical matches, then expand
one relationship hop in either direction. Retrieval includes source paths, line
ranges and the relationship that selected a neighbor. These are static syntax
relationships, not proven runtime call targets or model-inferred knowledge.
There are no embeddings or external graph services.

Conversational agent turns automatically refresh bounded graph evidence. The
`graph_search` tool is also available for explicit queries. Retrieval is untrusted
source evidence, never a replacement for project instructions or file read checks.
The cache is keyed to the resolved workspace root under `~/.agentharness/graphs`.
Changes and deletions invalidate it; unchanged contents avoid parsing again.

Index scope: 600 eligible files, 256 KB per file, approximately 10 MB total.
Supports common text/code extensions; binary media is not indexed. Git ignores,
hidden paths, dependency directories, symlinks escaping the root and common
secret filenames are excluded. This is not a secret-content detector. Python
gets symbol relationships; other code languages currently get file excerpts.
Long functions/documents have bounded excerpts, not complete semantic indexing.
Retrieval is capped at 4,500 characters. A rebuildable cache failure is logged
without failing the conversation. Caches and artifacts can be removed manually
when sessions/projects are retired; there is no automatic retention policy yet.

## Creative engines

The installed Craft AppImages include separate CLI executables. Run
`python scripts/setup_creative_apps.py` to expose those existing binaries; no
new download is needed. PdfCraft's installed binary is named `printcraft-cli`.
Nessa launches one headless MCP engine on demand, preserving its state across
turns in the same process. Explicitly save documents before exit. After restarting,
reopen saved documents; in-memory unsaved app state is not durable.

| App | Workflow |
|---|---|
| Blender | 3D assets, rigging, animation, lighting and rendering via project Python scripts |
| PhotoCraft | Painting, retouching, raster layers |
| VectorCraft | Paths, illustration, typography, SVG/PDF |
| FilmCraft | Media bins, editing, timeline, audio and video delivery |
| EffectCraft | Compositing, motion graphics, effects and keyframes |
| LightCraft | Photo development, image library and grading |
| DesignCraft | Page layout and publication |
| PdfCraft | PDF inspection, annotations and assembly |
| AnharmonicStudio | Existing adapter for supported music/beat operations |
| FFmpeg / ffprobe | Encoding and independent media inspection |

`creative_apps` reports installation. `creative_tools` discovers an app's exact
schemas with query filtering and pagination. `creative_call` invokes the discovered
tool through existing action permissions and operation receipts. `blender_run`
runs an existing project script, using two threads, factory startup, disabled
auto-execution and a bounded timeout. Script errors return failure via Blender's
Python exit code. `media_probe` reads real media metadata. Like the existing
shell tool, a permitted script or app command can perform file operations; these
engines are not OS-sandboxed. Inspect the requested action before approving it.

The connections control headless engines. They do not attach to an already-open
GUI document; GUI control requires a separately configured app bridge. Current
adapters expose the apps' actual tools and errors, not feature parity with Adobe.
MCP image/audio results are saved to evidence when supported, with a local artifact
path. The text model is not automatically given image/audio understanding.
Visual/audio quality review remains explicit and must not be inferred from metadata.

Eight production skills cover briefs/shot planning, Blender animation, images,
vector/layout, editing, compositing, sound, and delivery. Relevant production
skills are selected on entry to a creative work task. They specify review steps;
unregistered visual/audio checks are guidance, not automatic approval gates.

Example request:

> Use the creative-production and blender-animation skills. Create a two-second
> preview of a rotating product, save the editable scene, render a low-resolution
> image sequence, and validate the exported video. Show me the preview before
> proceeding to a final render.

`examples/creative/blender_preview.py` is an executable smoke scene for the
`blender_run` tool. It creates an editable `.blend` and 12 PNG frames at 320×180,
12 fps. Rendering a professional film requires assets, iteration, shot review,
sound and a suitable render budget; installing these tools does not establish
that quality level.

## Validation on this machine — 2026-10-08

- Blender 4.5.14 LTS installed under `~/.local/opt/blender`, verified against the
  official SHA-256 manifest, linked as `~/.local/bin/blender`.
- All seven installed Craft MCP servers initialized and returned catalogues:
  PhotoCraft 20, VectorCraft 25, FilmCraft 17, EffectCraft 22, LightCraft 12,
  PdfCraft 123, DesignCraft 25 tools. They are loaded on demand, not all added to
  each model request.
- Real raster creation/export, native PhotoCraft save/reopen, SVG/PDF export,
  LightCraft import/export, DesignCraft page export/save/reopen, EffectCraft
  composition/render/save/reopen, PdfCraft open/inspect and FilmCraft media
  import/project save succeeded.
- Blender rendered all 12 frames; FFmpeg encoded an H.264 preview. ffprobe
  confirmed 320×180, 12 fps, 12 frames and 1.000 seconds. No audio was expected.
  One frame was visually inspected; this is a functional smoke scene, not a
  judged movie or a complete visual/audio quality evaluation.
- Reports and artifacts: `/home/alabs/nessa-creative-validation/`.
- Regression tests cover graph hops/freshness/scope, chat injection, terminal
  sanitization/streaming, permission denial, schema pagination, runtime failure,
  preview persistence and existing agent behavior.

Official interfaces: [FilmCraft control protocol](https://github.com/storytold/filmcraft/blob/main/docs/control-protocol.md),
[PhotoCraft](https://github.com/storytold/photocraft),
[VectorCraft MCP](https://github.com/storytold/vectorcraft/blob/main/docs/mcp.md),
[EffectCraft](https://github.com/storytold/effectcraft),
[LightCraft MCP](https://github.com/storytold/lightcraft/blob/main/docs/mcp.md),
[DesignCraft MCP](https://github.com/storytold/designcraft/blob/main/docs/mcp.md),
[PdfCraft](https://github.com/storytold/pdfcraft),
[Blender Python API](https://docs.blender.org/api/4.5/).

## Studio operating layer

[Studio orchestration and measured improvement](STUDIO_OPERATING_SYSTEM.md) adds
persistent task dependencies, actual asset snapshots, artifact checks, interruption
recovery and scoped workflow experiments. Use `/production` in terminal chat.
