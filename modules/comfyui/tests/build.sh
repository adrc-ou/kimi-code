#!/usr/bin/env bash
set -euo pipefail
set -a
# shellcheck disable=SC1091
source modules/comfyui/backend/backend.env
set +a
docker buildx build --platform linux/amd64 -f modules/comfyui/backend/Dockerfile.cuda \
  --build-arg COMFYUI_VERSION=v0.35.0 \
  --build-arg COMFYUI_COMMIT=40c4fcdf513a4523e39d54a9d391908af8df8171 \
  --build-arg TORCH_VERSION --build-arg TORCHVISION_VERSION \
  --build-arg TORCHAUDIO_VERSION --build-arg PYTORCH_CUDA_INDEX_URL .
