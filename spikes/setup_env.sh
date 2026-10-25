#!/bin/bash
# Environment bootstrap for transcue.
# (Re)creates the Python venv (spikes/.venv-ml) and points HuggingFace at the
# project's model cache (models-cache/), so models are downloaded only once.
# Run it with bash (sourcing it would leave `set -e` in your shell).
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
SPIKES="$PROJ/spikes"
cd "$SPIKES"

# 1) HuggingFace cache inside the project.
export HF_HOME="$PROJ/models-cache/huggingface"
echo "HF_HOME=$HF_HOME"
ls "$HF_HOME/hub" 2>/dev/null || echo "NOTE: no models in $HF_HOME yet — download them (README)"

# 2) Recreate venv if the interpreter symlink is dead (happens across accounts).
if [ ! -x ".venv-ml/bin/python" ] || ! .venv-ml/bin/python -c "" 2>/dev/null; then
  echo "Recreating .venv-ml ..."
  rm -rf .venv-ml
  uv venv .venv-ml --python 3.12
  # shellcheck disable=SC1091
  . .venv-ml/bin/activate
  if [ -f requirements-lock.txt ]; then
    uv pip install -r requirements-lock.txt
  else
    uv pip install mlx-lm mlx-audio kokoro-onnx soundfile torch numba "misaki[en]" sounddevice
  fi
else
  echo ".venv-ml already usable"
  # shellcheck disable=SC1091
  . .venv-ml/bin/activate
fi

[ -f "$PROJ/src/config.toml" ] || { cp "$PROJ/src/config.example.toml" "$PROJ/src/config.toml"; echo "src/config.toml を見本から作りました"; }
echo "OK. Activate with:  export HF_HOME=$HF_HOME && source $SPIKES/.venv-ml/bin/activate"
