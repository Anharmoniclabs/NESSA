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

The Tkinter window uses Qwen 1.5B (`nessa-chat:latest`) for conversation on
`127.0.0.1:11436`, and the existing LFM model for project work on
`127.0.0.1:11435`. Chat replies stream into the window as they arrive.
The small model calls `start_work` to hand project requests to the larger model
inside the same agent loop; user approval and private workspaces still apply.
The `nessa-chat.service` preloads the small model and keeps it resident
(`OLLAMA_KEEP_ALIVE=-1`), separate from the large model’s request queue.
It uses approximately 1.37 GB of additional model memory on this machine.
The small model has a 384-token reply limit; project-model replies retain their existing budget.
It opens ready to chat: type your first message without choosing a file or project.
New conversation also starts an ordinary chat immediately. Use the optional
Project chat button when you want to work on an existing codebase.
The desktop layout includes a collapsible, searchable conversation sidebar,
a centered composer, prompt suggestions, and formatted replies. Activity and
Changes open in a separate workspace window. Ctrl+N starts a new chat;
unfinished messages are retained while switching between chats in the same window.
It includes saved conversations, live activity, plan approval,
and patch/evidence review. Start the backend with
`systemctl --user start --no-block nessa-ollama.service nessa-chat.service` if needed.
Conversations are stored under `~/.agentharness/desktop-chats/`.
Enter sends; Shift+Enter inserts a new line. Stop interrupts fast chat at the next
streamed token. For the existing project model it takes effect at the next
model response or tool boundary.
Project changes remain in private copies; the Changes tab shows the resulting patch.
The desktop UI uses the code in this checkout, which may differ from an older installed CLI.

The fast model is built from `configs/Modelfile.chat-i3`; `scripts/warm_chat.py`
preloads it through the local API. To change models or endpoints, use
`--chat-model` / `--chat-base-url` and `--model` / `--base-url`.
The `nessa-gui` launcher starts both services without blocking window startup.

## Harness architecture

See the [wiring audit](docs/HARNESS_WIRING_AUDIT.md) for the implemented
plan/tool/observation/verification/recovery loop and the remaining architectural
gaps. Activated skill recipes and retrieved project lessons persist across
compaction and resume. Optional desktop review is available with
`--review-model MODEL`; it is disabled by default and cannot override checks.
