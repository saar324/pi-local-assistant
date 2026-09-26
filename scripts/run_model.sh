#!/usr/bin/env bash
set -euo pipefail

llama_dir=${LLAMA_CPP_DIR:-"$HOME/llama.cpp"}
model_path=${MODEL_PATH:-"$HOME/models/Qwen3.5-0.8B-Q4_K_M.gguf"}

if [[ ! -x "$llama_dir/build/bin/llama-server" || ! -f "$model_path" ]]; then
  echo "Run scripts/setup_pi.sh first." >&2
  exit 1
fi

# Core dumps may contain private prompt text.
ulimit -c 0
exec "$llama_dir/build/bin/llama-server" \
  --model "$model_path" \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 2048 --parallel 1 \
  --threads 2 --threads-batch 2 \
  --batch-size 64 --ubatch-size 64 \
  --reasoning off --no-webui
