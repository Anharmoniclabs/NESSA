#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v ollama >/dev/null || { echo 'Install Ollama for your OS first, then run this script again.' >&2; exit 1; }
ollama list >/dev/null || { echo 'Start Ollama (ollama serve), then run this script again.' >&2; exit 1; }
ollama pull qwen2.5-coder:1.5b-instruct-q4_K_M
ollama create nessa-i3 -f configs/Modelfile.i3
python -m agentharness doctor --profile laptop-i3-12gb
python scripts/smoke_local.py --profile laptop-i3-12gb
