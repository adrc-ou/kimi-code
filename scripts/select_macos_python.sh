#!/usr/bin/env bash
set -euo pipefail

compatible_python() {
  "$1" -c 'import platform, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and platform.machine() == "arm64" else 1)' >/dev/null 2>&1
}

if [[ -n "${COMFYUI_MACOS_PYTHON:-}" ]]; then
  if compatible_python "${COMFYUI_MACOS_PYTHON}"; then
    command -v "${COMFYUI_MACOS_PYTHON}"
    exit 0
  fi
  echo "COMFYUI_MACOS_PYTHON must point to an executable arm64 Python 3.12." >&2
  exit 1
fi

for candidate in python3.12 python3; do
  if command -v "${candidate}" >/dev/null 2>&1 && compatible_python "${candidate}"; then
    command -v "${candidate}"
    exit 0
  fi
done

root=$(cd "$(dirname "$0")/.." && pwd -P)
: "${HARNESS_RUNTIME_DIR:?Run ./start.sh to provision the instance-local Python runtime}"
# The launcher holds the instance lock throughout provisioning and use.
metadata=$(python3 - "${root}/dependencies.lock.json" <<'PY'
import json, re, sys
pin = json.load(open(sys.argv[1]))["downloads"]["macos-python"]
assert re.fullmatch(r"sha256:[0-9a-f]{64}", pin["digest"])
assert pin["url"].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/")
print(pin["url"])
print(pin["digest"].removeprefix("sha256:"))
PY
)
url=${metadata%%$'\n'*}
digest=${metadata#*$'\n'}
base="${HARNESS_RUNTIME_DIR}/python"
destination="${base}/${digest}"
interpreter="${destination}/python/bin/python3.12"
if [[ -e "${destination}" ]]; then
  if compatible_python "${interpreter}"; then
    printf '%s\n' "${interpreter}"
    exit 0
  fi
  echo "Managed Python is invalid: ${destination}. Set COMFYUI_MACOS_PYTHON to an arm64 Python 3.12 executable." >&2
  exit 1
fi

umask 077
mkdir -p "${base}"
stage=$(mktemp -d "${base}/install.XXXXXXXX")
cleanup() { find "${stage}" -depth -delete; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "Installing verified arm64 Python 3.12 for this instance..." >&2
curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
  "${url}" --output "${stage}/python.tar.gz"
actual=$(shasum -a 256 "${stage}/python.tar.gz")
if [[ "${actual%% *}" != "${digest}" ]]; then
  echo "Standalone Python checksum mismatch; installation cancelled." >&2
  exit 1
fi
mkdir "${stage}/runtime"
tar -xzf "${stage}/python.tar.gz" -C "${stage}/runtime"
if ! compatible_python "${stage}/runtime/python/bin/python3.12"; then
  echo "Downloaded Python does not run as arm64 Python 3.12." >&2
  exit 1
fi
mv "${stage}/runtime" "${destination}"
printf '%s\n' "${interpreter}"
