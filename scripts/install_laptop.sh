#!/usr/bin/env bash
# Run as your desktop user, not with sudo. sudo is used only for missing OS packages.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $(id -u) == 0 ]]; then
  echo 'Run this as your normal desktop user, without sudo.' >&2
  exit 1
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'This installer targets your x86_64 Linux laptop (CachyOS/Mint/Ubuntu).' >&2
  exit 1
fi
missing=0
for dependency in python3 curl zstd tar systemctl git; do
  command -v "$dependency" >/dev/null || missing=1
done
if (( missing )); then
  echo 'Installing missing runtime prerequisites; your package manager may ask for sudo.'
  if command -v pacman >/dev/null; then
    sudo pacman -Syu --needed python curl zstd tar systemd git
  elif command -v apt-get >/dev/null; then
    sudo apt-get update
    sudo apt-get install python3 curl zstd tar systemd git
  else
    echo 'Install Python 3.10+, curl, zstd, tar and systemd, then rerun.' >&2
    exit 1
  fi
fi
exec python3 scripts/install_laptop.py "$@"
