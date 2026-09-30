#!/usr/bin/env bash
# Use the installed Ollama/model. Keep every run under $HOME, not /tmp.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
model=${AGENT_MODEL:-qwen2.5-coder:1.5b}
base=${AGENT_BASE_URL:-http://127.0.0.1:11434/v1}
runs=${NESSA_RUNS_DIR:-"$HOME/nessa-three-stage-runs"}
mkdir -p "$runs"
if ! command -v ollama >/dev/null 2>&1; then
  echo 'Ollama is not installed. Start your existing local model server before running this smoke test.' >&2
  exit 1
fi
if [[ "$base" == http://127.0.0.1:11434/v1 || "$base" == http://localhost:11434/v1 ]]; then
  if ! OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; then
    echo "Starting installed Ollama on loopback; log: $runs/ollama.log"
    nohup env OLLAMA_HOST=127.0.0.1:11434 ollama serve >>"$runs/ollama.log" 2>&1 < /dev/null &
    ready=0
    for _ in {1..30}; do
      if OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; then ready=1; break; fi
      sleep 1
    done
    if [[ "$ready" != 1 ]]; then echo "Ollama did not become ready. See $runs/ollama.log" >&2; exit 1; fi
  fi
  if ! OLLAMA_HOST=127.0.0.1:11434 ollama show "$model" >/dev/null 2>&1; then
    printf 'Model is not present. Pull it explicitly, then rerun: ollama pull %q\n' "$model" >&2
    exit 1
  fi
fi
work="$runs/smoke-$(date -u +%Y%m%dT%H%M%SZ)-$$"
echo "Evidence will remain at $work/evidence"
python -m agentharness.three_stage examples/three_stage_smoke \
  'Fix add(a, b) so it returns the sum for positive, negative and zero values. Preserve the tests.' \
  --work "$work" --base-url "$base" --model "$model" --text-tools \
  --check "tests=\"$(command -v python)\" -m unittest discover -s checks -p addition_smoke.py -q" "$@"
