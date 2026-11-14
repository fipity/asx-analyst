#!/usr/bin/env bash
# One-off: build the FinBERT int8 ONNX model into ./data/models/finbert.
# Uses a throwaway python container with CPU-only torch; nothing is kept but the model.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=data/models/finbert
mkdir -p "$OUT"
docker run --rm \
  -v "$PWD/tools:/tools:ro" -v "$PWD/$OUT:/out" \
  -e HF_HUB_DISABLE_TELEMETRY=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  python:3.12-slim bash -c '
    pip install --quiet --no-cache-dir --user \
      --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 &&
    pip install --quiet --no-cache-dir --user \
      transformers==4.46.3 onnx==1.17.0 onnxruntime==1.20.1 numpy==2.1.3 &&
    python /tools/export_finbert.py /out'
ls -la "$OUT"
