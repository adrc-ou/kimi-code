#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../../.." && pwd -P)
cd "${root}"
set -a
# shellcheck disable=SC1091
source modules/comfyui/backend/backend.env
set +a
# Which release to build, and which requirements it installs with, both come from the reviewed
# document rather than from this script: a rebuild after the baseline moves has to test the release
# that moved in, and the digest is the claim the Dockerfile checks the lock against.
{
  read -r version
  read -r commit
  read -r requirements
} < <(python3 - <<'PY'
import json
from pathlib import Path

document = json.loads(Path("modules/comfyui/backend/compatibility.json").read_text())
baseline = next(p["baseline"] for p in document["platforms"] if p["platform"] == "wsl2-x86_64")
print(baseline["comfyui_version"])
print(baseline["comfyui_commit"])
print(baseline["requirements_sha256"])
PY
)
runtime=${HARNESS_RUNTIME_DIR:-"${root}/.local/runtime/comfyui-build-check"}
mkdir -p "${runtime}"
python3 modules/comfyui/resolve_locks.py --platform wsl2-x86_64 --version "${version}" \
  --commit "${commit}" --requirements-sha256 "${requirements}" --runtime-dir "${runtime}" \
  --output "${runtime}/comfyui/lock-path"
lock=$(<"${runtime}/comfyui/lock-path")
# The same staging the launcher's prepare hook does, so this build exercises the path an operator's
# launch takes rather than a second route to the same image.
mkdir -p "${runtime}/comfyui/build"
cp -f -- "${lock}" "${runtime}/comfyui/build/requirements.lock"
docker buildx build --platform linux/amd64 -f modules/comfyui/backend/Dockerfile.cuda \
  --build-context "comfy-locks=${runtime}/comfyui/build" \
  --build-arg COMFYUI_VERSION="${version}" \
  --build-arg COMFYUI_COMMIT="${commit}" \
  --build-arg COMFYUI_REQUIREMENTS_SHA256="${requirements}" \
  --build-arg TORCH_VERSION --build-arg TORCHVISION_VERSION \
  --build-arg TORCHAUDIO_VERSION --build-arg PYTORCH_CUDA_INDEX_URL .
