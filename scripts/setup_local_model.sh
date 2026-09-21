#!/usr/bin/env bash
# Install Ollama (source and installers: https://github.com/ollama/ollama) and pull a
# local fallback model.
# Usage:   scripts/setup_local_model.sh [model, default qwen2.5:7b]
# Notes:   The Ollama program is published on GitHub; model weights are served from the
#          Ollama library (ollama.com/library), which is not on GitHub.
set -euo pipefail
MODEL="${1:-qwen2.5:7b}"

if ! command -v ollama >/dev/null 2>&1; then
  echo ">> ollama not found, installing..."
  if [[ "${OSTYPE:-}" == darwin* ]] && command -v brew >/dev/null 2>&1; then
    brew install ollama
  elif [[ "${OSTYPE:-}" == linux* ]]; then
    curl -fsSL https://ollama.com/install.sh | sh
  else
    echo "Please install Ollama manually from https://github.com/ollama/ollama/releases and run this script again." >&2
    exit 1
  fi
fi

if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo ">> starting the ollama service..."
  nohup ollama serve >"${TMPDIR:-/tmp}/ollama.log" 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo ">> pulling model $MODEL (about 4-5 GB, this takes a while)..."
ollama pull "$MODEL"
echo ">> Done. Verify it under Settings -> Providers -> Ollama by clicking \"Test\" on the model."
