#!/usr/bin/env bash
set -euo pipefail

files=(
  server.py
  utils.py
  gateway.py
  embedding_engine.py
  memory_diffusion.py
  memory_moments.py
  memory_nodes.py
  recall_policy.py
  reranker_engine.py
)

existing=()
for f in "${files[@]}"; do
  if [[ -f "$f" ]]; then
    existing+=("$f")
  fi
done

if [[ ${#existing[@]} -eq 0 ]]; then
  echo "No known Python files found for quick verification."
  exit 0
fi

python -m compileall "${existing[@]}"
