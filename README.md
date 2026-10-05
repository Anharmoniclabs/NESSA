# NESSA

Local-first autonomous coding agent work.

Desktop chat can call live `weather`, `web_search`, and `web_fetch` tools, and
read/OCR documents with `local_read` or extract regex fields/tables with
`local_extract`. Local read roots are Projects, Documents, Downloads, Desktop,
and Pictures under your home directory. `runtime_info` reports available tools
and OCR dependencies. Read-only calls do not require a project plan. Commands,
builds, managed processes, and edits enter the existing project workflow; edits
remain in private snapshots and completion still requires verification.

Weather uses [Open-Meteo](https://open-meteo.com/en/docs); web search uses Bing's
public RSS results. These tools require network access, while model inference
remains local. Provider failures are reported as tool errors. The fast desktop
model receives native tool schemas even when the coding model uses JSON tools.

News requests use a separate dated-article feed, preserve the user's topic, reject
homepages and off-topic/stale results, and attempt publisher retrieval. Short empty
date windows can expand to seven days with an explicit notice. News answers render
article titles, dates, source excerpts and links from retrieval evidence; blocked
pages are labeled feed-only, and related coverage is grouped. No qualifying sources
means an incomplete request, not a model-generated news answer.

| Folder | What it is |
|---|---|
| [`agentharness/`](agentharness/README.md) | Coding agent harness: inspect → plan → approve → edit a private copy → verify → patch + evidence. Also includes OCR/regex document extraction into tables. Works with Ollama or vLLM, standard library only. |
| [`kaggle/gemma4_submission/`](kaggle/gemma4_submission/README.md) | Google Gemma 4 Developer Agent competition: declarative `submission/` for the `swegemma` harness, a validator/packager, and Kaggle notebook cells (start server → write submission → local eval). |

```bash
python -m unittest discover -v  # harness tests; no GPU or pytest needed
python -m unittest discover -s kaggle/gemma4_submission -v
python -m agentharness doctor
```

## Message-driven local agent

NESSA now supports terminal chat, durable follow-ups/resume, operation receipts,
context digests, live process polling and stricter verification completion.

- [Message and tool workflow, diagrams and implementation boundaries](docs/MESSAGE_TOOL_WORKFLOW.md)
- [i3 / 12 GB laptop setup and real-model acceptance](docs/LAPTOP_I3_12GB.md)

```bash
bash scripts/setup_i3.sh
python -m agentharness chat /path/to/project --profile laptop-i3-12gb
```

## Install LFM2.5 on the HP i3 / 12 GB laptop

```bash
bash scripts/install_laptop.sh
~/.local/bin/nessa chat /path/to/project
```

[Complete installer, runtime settings, reports and troubleshooting](docs/LFM_LAPTOP_INSTALL.md).
This is a separate `lfm-i3-12gb` profile; the earlier Qwen 1.5B profile remains available.
The installer runs real-model acceptance locally before reporting readiness.

## Native desktop chat

```bash
python -m agentharness.gui
# Or open a project immediately:
python -m agentharness.gui /path/to/project
```

The Tkinter window uses local LFM2.5 (`nessa-lfm:latest`) for both conversation
and project work at `127.0.0.1:11435`. Replies stream as they arrive. Ordinary
chat uses a compact prompt and a small selection of tools; `start_work` enters
the full coding workflow in the same loop, with approval and private workspaces.
The `configs/local-performance.conf` systemd drop-in keeps LFM resident, warms it
at service startup, and sets `OLLAMA_NO_CLOUD=1`. Install it under
`~/.config/systemd/user/nessa-ollama.service.d/`, then reload/restart the service.
The previous Qwen chat service is optional and disabled on this installation.
No cloud model or subscription is required. Default clients reject cloud model aliases.
It opens ready to chat: type your first message without choosing a file or project.
New conversation also starts an ordinary chat immediately. Use the optional
Project chat button when you want to work on an existing codebase.
The desktop layout includes a collapsible, searchable conversation sidebar,
a centered composer, prompt suggestions, and formatted replies. Activity and
Changes open in a separate workspace window. Ctrl+N starts a new chat;
unfinished messages are retained while switching between chats in the same window.
It includes saved conversations, live activity, plan approval,
and patch/evidence review. Start the backend with
`systemctl --user start --no-block nessa-ollama.service` if needed.
Conversations are stored under `~/.agentharness/desktop-chats/`.
Before every desktop query, Nessa reads the visible conversation and rebuilds
`memory-cache.json` in that chat folder. A bounded selection of recent turns,
relevant earlier messages, and user feedback is supplied to the model, including
turns handled outside the model loop. Corrections such as “remember” or “take note”
receive priority. The original log is preserved; repeated assistant text is
deduplicated in the retrieved excerpts and is not treated as verified knowledge.
Global user lessons also apply to chats without an attached project. This is
persistent context retrieval, not model-weight training; transcript retrieval stays within its chat.
Explicit `remember KEY: NOTE` commands save versioned user-authored lessons: project-scoped
in project chats, otherwise global. `forget KEY` removes a lesson from future recall while
retaining its audit history. Newer versions replace earlier ones. Both desktop and CLI
load these lessons without running another model. See [ANRII integration](docs/ANRII_INTEGRATION.md).
Restart the desktop app after updating the code to use this behavior.
Enter sends; Shift+Enter inserts a new line. Stop interrupts fast chat at the next
streamed token. For the existing project model it takes effect at the next
model response or tool boundary.
Project changes remain in private copies; the Changes tab shows the resulting patch.
The desktop UI uses the code in this checkout, which may differ from an older installed CLI.

LFM is configured by `configs/Modelfile.lfm-i3`; `scripts/warm_chat.py
--model nessa-lfm:latest --url http://127.0.0.1:11435` preloads it through the local API. To change models or endpoints, use
`--chat-model` / `--chat-base-url` and `--model` / `--base-url`.
The `nessa-gui` launcher starts the local LFM service without blocking window startup.

Original writing stays in chat: broad book requests should ask for a genre/topic
or offer an outline, and specified scenes should produce prose. Restart the desktop
app after changing its Python prompts; rebuilding the Ollama model is unnecessary.
Run `python scripts/smoke_chat.py` with the chat service running to check greeting,
book assistance, scene drafting and name recall through the actual desktop controller.
It prints and saves real replies and heuristic checks, exits nonzero on errors,
refusals or unwanted approval requests, and uses a separate temporary conversation.
Read the saved prose to assess quality; this is not a general model benchmark.

Long chat answers use up to eight saved passes per turn. Desktop passes request
640 tokens with a 180-second stream deadline. Length-limited and interrupted prose
is retained; a `[[CONTINUE]]` section marker also requests the next pass. Two
interrupted passes or the pass cap pause with a saved continuation point. Send
`continue` to resume, including after reopening the app. Stop saves the visible
prose; incomplete native tool calls are never resumed as actions. Streaming text
is checkpointed about once per second (an abrupt crash can lose the last second).
Compaction retains bounded excerpts of the initial brief, early responses and
latest continuation; full text is retained in chat/evidence files. This is not
unlimited model memory, and model-generated story consistency still needs review.
Run `python3 scripts/smoke_continuation.py` for a live island-romance outline and
restart/recall check. See [Odysseus assessment](docs/ODYSSEUS_ASSESSMENT.md) for
the upstream research and this laptop's model constraints.

## Working on your projects

In the desktop app, a project chat edits a private copy. After a `verified` or
`unverified` result, press **Apply to project** in the Changes window, or send
`apply changes`. Nessa refuses (and writes nothing) if any target file changed in your
project since it was copied, so your own edits are never overwritten. Applied files
become the new baseline, so the next patch contains only newer work.

Per-project settings live in `agentharness.toml` at the project root
([example](configs/agentharness.example.toml)); check one with
`python -m agentharness config /path/to/project --mcp`.

- `[checks]` registers commands (tests, lint, architecture rules); `[verification]` picks
  which run after edits and before a run is accepted. Explicit CLI flags still win.
- Skills whose `skill.json` lists a registered check name now enforce it at finish.
- `[dev.NAME]` presets: `dev_start(name="app")` with no argv; `dev_health` probes a local
  health URL. Dev processes are recorded with their kernel start time and reattached after
  a restart or crash; desktop chats keep them running between turns until the window closes.
- `[mcp.NAME]` connects MCP servers over stdio or streamable HTTP. Tools appear as
  `mcp__NAME__TOOL`; only read-only ones are usable before plan approval. A crashed
  server returns an error observation and is restarted on the next call. Remote URLs need
  `allow_remote = true`.
- Every run writes `timing.json` and an OTLP/JSON `trace.json` to its evidence directory.
  `[telemetry] otlp_endpoint` also exports to a local collector (names and timings only).

## Harness architecture

See the [wiring audit](docs/HARNESS_WIRING_AUDIT.md) for the implemented
plan/tool/observation/verification/recovery loop and the remaining architectural
gaps. Activated skill recipes and retrieved project lessons persist across
compaction and resume. Optional desktop review is available with
`--review-model MODEL`; it is disabled by default and cannot override checks.
