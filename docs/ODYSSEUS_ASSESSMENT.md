# Odysseus and Nessa — 2026-10-04

## Findings

The [official Odysseus repository](https://github.com/odysseus-dev/odysseus)
describes a self-hosted workspace with chat/tools, research, documents,
email/calendar, persistent memory, model comparisons and hardware-aware model
selection. Its declared license is AGPL-3.0-or-later. This change copies no
upstream implementation; Nessa retains its existing single-agent controller.

The [official Ajax page](https://data.pewdiepie.com/) currently says the model is
coming soon, with no fixed release date. It identifies Ajax as a fine-tuned
Qwen3.5-9B trained for Odysseus tool workflows. A public downloadable Ajax artifact
was not verified. Ajax is a fine-tune; Qwen is its underlying model family.

The [Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B) documents
text/vision, reasoning, coding and agent use, and links quantized formats for
llama.cpp/Ollama. These are upstream capabilities, not results measured on Nessa.
Odysseus-specific tool training does not automatically transfer to Nessa's schemas.

## Hardware fit

Observed: Intel i3-1115G4, two cores/four threads, about 11 GiB usable RAM,
1.8 GiB available at inspection, 7.5 GiB swap occupied. Nessa's configured
chat model is Qwen2.5-1.5B, CPU-only. The NVIDIA utility was unavailable;
no GPU inference was verified. Python is 3.14.7; Docker was not found in PATH.

The [native setup guide](https://github.com/odysseus-dev/odysseus/blob/dev/website/setup.md)
supports Linux with Python 3.11+ and existing Ollama endpoints. Thus the workspace
appears compatible in principle, subject to installing its dependencies and
testing them on this Python version. It has not been installed or launched here.

Estimated model memory: 9 billion weights at 16 bits need about 18 GB before
runtime/cache overhead, exceeding physical RAM. At 4 bits the raw arithmetic
minimum is about 4.5 GB; real quantized files, mixed-precision tensors, runtime
and KV cache add more. A short-context quantized model may fit after freeing
several GB and unloading other models, but current memory pressure makes it a
poor default. CPU latency is unmeasured and likely worse than the current 1.5B
model. Do not infer performance from nominal context length or parameter count.

## Useful harness ideas

| Idea | Evidence | Nessa action |
| --- | --- | --- |
| Preserve durable continuity when prompts are shortened | [Context compactor](https://github.com/odysseus-dev/odysseus/blob/dev/src/context_compactor.py) summarizes older messages and keeps recent exchanges | Implemented bounded original-user excerpts, early assistant anchors and a continuation cursor without requiring another model call |
| Treat long writing as ongoing work | [Agent loop](https://github.com/odysseus-dev/odysseus/blob/dev/src/agent_loop.py) routes long writing to documents | Implemented short, checkpointed chat passes; a dedicated manuscript editor is a future feature |
| Fit model choices to hardware | Official repository's Cookbook feature | Keep the small chat model; evaluate a quantized alternative in a separate controlled benchmark before switching |

Nessa additionally retains text on a stream deadline/disconnect, publishes each
completed pass, supports restart/Stop continuation, caps automatic passes, and
keeps incomplete tool calls out of the continuation path. A model can still omit
its continuation marker or contradict earlier prose; passing harness tests does
not prove semantic completeness or unlimited recall.

## Verification limits and live acceptance

Focused tests cover timeout retention, token-limit continuation, multiple replies,
saved cursor resume, compaction, crash recovery and tool-call separation.
The broad suite encounters socket/installer failures in this restricted execution
environment. Direct access to the local model endpoint also fails with
`Operation not permitted`; no fresh tokens/sec or writing-quality score is claimed.

From a normal terminal with Nessa's services running:

```bash
cd /home/alabs/Projects/NESSA
python3 scripts/smoke_continuation.py
```

The script creates an isolated conversation, generates the island-romance outline,
reloads the application between follow-ups and checks character/setting recall.
It saves actual prose and heuristic results. Read the outline to assess whether
all twelve chapters and the requested ending were delivered coherently.
