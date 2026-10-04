# LFM2.5 laptop installation

Target: HP 14-dq2031wm, i3-1115G4, 12 GB RAM, 500 GB SSD, running
CachyOS/Arch or Mint/Ubuntu with a systemd desktop user session. Run as your
normal user, not root. Installation needs internet and at least 16 GiB free disk.

## Install

In your current NESSA checkout:

```bash
git switch main
git pull --ff-only
bash scripts/install_laptop.sh
```

If you do not have a checkout yet, clone the private repository using your normal
GitHub authentication, or download/extract its ZIP while signed into GitHub.
Then open a terminal in the NESSA folder and run the installer command above.

The bootstrap installs missing Python 3.10+, Git, curl, tar, zstd and systemd
prerequisites through pacman or apt. Package installation may request sudo. On
Arch/CachyOS the prerequisite path uses a full system upgrade (`pacman -Syu`) to
avoid a partial upgrade; the package manager asks for confirmation.

The Python installer then:

1. Checks the desktop user service manager, free space and port availability.
2. Downloads the official x86_64 Ollama runtime into your user-owned NESSA folder.
3. Copies NESSA into an installed application directory, retaining previous installs
   as timestamped backups. Your source checkout and projects are not overwritten.
4. Creates `nessa-ollama.service`, a user service bound to `127.0.0.1:11435`.
5. Downloads `lfm2.5:8b-a1b-q4_K_M` (about 5.2 GB) and creates `nessa-lfm:latest`.
6. Creates `~/.local/bin/nessa` and checks model connectivity.
7. Runs real-model coding acceptance. The installation report says `ready` only
   after the task is independently verified. Failure exits nonzero and records why.

No system-wide Ollama service is replaced. NESSA keeps its own model directory.
The runtime archive is downloaded from Ollama over HTTPS; its installed version
and model digests returned by Ollama are recorded. It is not a pinned runtime release.

## Start

```bash
~/.local/bin/nessa chat /path/to/your/project
```

Replace the project path with the folder you want NESSA to work on. It edits a
private copy and produces a patch. It asks for plan approval by default.
The interface is terminal chat, with tool/check progress, not a graphical UI.
Normal messages are answered as conversation, with follow-up context retained. Project
inspection and changes are driven by your requests. Baseline tests run after you approve
a work plan; a greeting does not start tests or create a repair plan. Model requests show
a waiting status and their elapsed time. Ctrl+C cancels the current task and returns to chat.

To verify conversation, remembered context and a coding request with the real model:

```bash
~/.local/bin/nessa smoke --conversation
```

Other commands:

```bash
~/.local/bin/nessa doctor
~/.local/bin/nessa run /path/to/project "Fix the failing test" --progress --show-diff
~/.local/bin/nessa resume /path/to/saved-work --message "Here is the missing detail"
~/.local/bin/nessa smoke
journalctl --user -u nessa-ollama.service -n 60 --no-pager
systemctl --user stop nessa-ollama.service
```

If `~/.local/bin` is already on PATH, use `nessa` directly. No shell startup file
is modified. The service starts at user login; it loads a model when requested.
After downloads, local inference works offline. Individual project tools may still
need network access or project-specific dependencies.

## Model and transport settings

- LFM2.5 8B-A1B Q4_K_M; all quantized weights still occupy memory.
- CPU execution, initially two threads, one loaded model and one parallel request.
- 8192-token server context; the coding client requests up to 3072 generated tokens.
- JSON text tool calls for the LFM coding profile. The adapter accepts explicit
  `name`/`arguments` calls and `commands`/`tool_name` envelopes, while retaining
  tool allowlists, argument validation and verification gates. It does not repair
  malformed source strings or execute examples inside unfinished reasoning.
- No artificial assistant reasoning prefill: the local LFM runtime can otherwise
  return a whitespace-only answer. Native tool support remains available to other profiles.
- Native OpenAI-compatible tool calls through Ollama's model-specific parser.
- NESSA `lfm-i3-12gb` profile: temperature 0.2, `reasoning_effort=none`, bounded
  observations, and an 18,000-character message/schema budget.
- The backend controls whether thinking can actually be disabled for its model.
  No raw Python-style model function call is evaluated as code.

8K is used rather than 4K to leave room for native tool definitions, observations
and generated responses. The character budget is not exact token accounting.
There is no promise of a particular tokens/second figure on this laptop.

## Reports, retry and updates

Installed files:

| Path under your home directory | Purpose |
|---|---|
| `.local/share/nessa/app/` | Installed NESSA code |
| `.local/share/nessa/runtime/` | Private Ollama binary and libraries |
| `.local/share/nessa/models/` | Downloaded model blobs |
| `.local/share/nessa/installation.json` | Status, backend version, model digests, hardware and acceptance location |
| `.local/share/nessa/acceptance/` | Real-model test results and evidence |
| `.config/systemd/user/nessa-ollama.service` | Local inference service |
| `.local/bin/nessa` | Command launcher |

Rerun the installer after a failed download; Ollama can reuse its downloaded model
blobs. Updating the checkout alone does not update the installed application:
rerun the installer to copy it. To also download a newer Ollama runtime:

```bash
bash scripts/install_laptop.sh --update-runtime
```

`--skip-acceptance` installs without running inference but records
`installed_unverified`, never `ready`. A failed acceptance does not delete the
installation; inspect its report and run `nessa smoke` after resolving the problem.
Smoke writes a fresh acceptance report; rerun the installer to refresh installation
readiness status. Keep only one agent operating each private work directory.

The automated development tests validate native HTTP routing and installation
orchestration with mocked downloads/systemd. They do not prove that a physical
laptop installation or LFM coding task has passed. That is the installer's final
on-device acceptance gate.

Official references:
- https://ollama.com/library/lfm2.5:8b-a1b-q4_K_M
- https://docs.ollama.com/linux
- https://docs.ollama.com/api/openai-compatibility
