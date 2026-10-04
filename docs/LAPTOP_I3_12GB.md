# Run NESSA on an i3 laptop with 12 GB RAM

Use **Qwen2.5-Coder-1.5B-Instruct Q4_K_M** as a conservative CPU starting point.
The official Ollama artifact is about 986 MB; total runtime memory also includes
KV cache, buffers and the OS. This selection favors a small working set and short
coding turns. It is not a claim of best benchmark quality or a measured speed on
an unspecified i3 generation. A 3B quantized coder is an optional quality comparison
once the 1.5B acceptance run passes; do not load both simultaneously on this setup.

Official references:
- https://ollama.com/library/qwen2.5-coder:1.5b-instruct-q4_K_M
- https://ollama.com/library/qwen2.5-coder
- https://docs.ollama.com/modelfile

## Setup

Requirements: Python 3.10+, Git, and a running Ollama installation. The core uses
Python's standard library. Install your project's test dependencies separately.

From the NESSA checkout:

```bash
bash scripts/setup_i3.sh
```

This downloads the model, creates the `nessa-i3` Ollama alias from
`configs/Modelfile.i3`, checks connectivity, and runs a real-model acceptance task.
Model download requires internet once. Normal inference uses localhost; there is
no cloud fallback. The script does not install OS packages or alter your services.
If Ollama is installed but stopped, run `ollama serve` in another terminal.

The Modelfile uses CPU execution, two inference threads, an 8192-token context and
1024-token responses. Two threads are a conservative starting point because the
i3 generation/core count is unknown. Adjust after measuring on your actual machine.

`laptop-i3-12gb` selects `nessa-i3:latest`, JSON-in-text tools, a maximum 18,000
serialized context/schema characters, 2500-character tool observations and a
6144-token reported-usage compaction threshold. Character limits are approximate;
the model server still owns actual tokenization/context enforcement.

## Use

```bash
python -m agentharness chat /path/to/project --profile laptop-i3-12gb
```

Enter a message at `you>`. NESSA prints tool/check activity, asks for plan approval,
and reports answers or patches. `/quit` exits. It edits a private workspace.
Use one chat process per workspace. No graphical browser UI is supplied by this update.

One task:

```bash
python -m agentharness run /path/to/project \
  "Fix the failing test and explain the change" \
  --profile laptop-i3-12gb --progress --show-diff
```

Resume the **work directory** printed by the prior run (not its `repo` subdirectory):

```bash
python -m agentharness resume /path/to/saved-work \
  --profile laptop-i3-12gb --message "Here is the missing detail"
```

If the original execution budget is exhausted, explicitly extend it:

```bash
python -m agentharness resume /path/to/saved-work \
  --profile laptop-i3-12gb --extra-steps 10 --extra-seconds 600
```

## Real-model acceptance and limits

```bash
python scripts/smoke_local.py --profile laptop-i3-12gb
```

The smoke task creates a subtraction bug, lets the real model inspect/plan/edit it,
and independently checks positive, negative and zero cases with a controller-owned
check outside the editable snapshot. It also confirms the original stayed unchanged.
It writes `acceptance.json` and complete evidence under `~/.agentharness/smoke/`.
A model merely saying “done” cannot pass it. Exit zero requires verified behavior.

Automated repository tests use scripted models/fake HTTP servers and real file,
process, check and CLI execution. They validate harness wiring, not local model
intelligence. This development workspace has no Ollama executable or laptop access;
real model speed, RAM use and acceptance on your laptop remain to be measured.
