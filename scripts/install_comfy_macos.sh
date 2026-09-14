#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: install_comfy_macos.sh ROOT WORKSPACE VERSION COMMIT PYTHON INSTANCE" >&2
  exit 2
fi

root=$1
workspace=$2
version=$3
commit=$4
python_command=$5
instance_id=$6
[[ "${instance_id}" =~ ^[0-9a-f]{16}$ ]] || { echo "invalid instance id" >&2; exit 2; }
base="${root}/.local/comfy-macos/${instance_id}"
releases="${base}/releases"
current="${base}/current"
old_link="${base}/old.$$"
new_link="${base}/current.$$"
installed=false
release_created=false

mkdir -p "${releases}"

# Reinstall when application, backend pins, custom requirements, installer, or
# the selected host Python executable changes.
fingerprint=$(
  {
    printf '%s\n' "${version}" "${commit}" "${python_command}"
    shasum -a 256 \
      "${root}/comfy/backend.env" \
      "${root}/comfy/requirements-macos.lock" \
      "${root}/comfy/requirements-custom.lock" \
      "${root}/scripts/install_comfy_macos.sh"
  } | shasum -a 256 | awk '{print $1}'
)
release="${releases}/${fingerprint}"

case "${release}" in
  "${base}"/releases/*) ;;
  *) echo "Unsafe release path: ${release}" >&2; exit 1 ;;
esac

cleanup() {
  if [[ "${installed}" != true && "${release_created}" == true && -d "${release}" ]]; then
    find "${release}" -depth -delete
  fi
  if [[ -L "${new_link}" ]]; then
    find "${new_link}" -delete
  fi
  if [[ ! -e "${current}" && ! -L "${current}" && -e "${old_link}" ]]; then
    mv -- "${old_link}" "${current}"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -x "${current}/venv/bin/python" \
      && -f "${current}/FINGERPRINT" \
      && "$(cat "${current}/FINGERPRINT")" == "${fingerprint}" ]]; then
  "${current}/venv/bin/python" -c \
    'import torch; assert torch.backends.mps.is_available(), "MPS unavailable"'
  trap - EXIT INT TERM
  exit 0
fi

if [[ -e "${release}" ]]; then
  find "${release}" -depth -delete
fi
mkdir -p "${release}/app"
release_created=true

git init "${release}/app"
git -C "${release}/app" remote add origin \
  https://github.com/Comfy-Org/ComfyUI.git
git -C "${release}/app" fetch --depth 1 origin "${commit}"
git -C "${release}/app" checkout --detach FETCH_HEAD
test "$(git -C "${release}/app" rev-parse HEAD)" = "${commit}"

"${python_command}" -m venv "${release}/venv"
"${release}/venv/bin/python" -m pip install --require-hashes \
  --requirement "${root}/comfy/requirements-macos.lock"
"${release}/venv/bin/python" -m pip install --require-hashes \
  --requirement "${root}/comfy/requirements-custom.lock"

find "${release}/app/models" -depth -delete
find "${release}/app/custom_nodes" -depth -delete
ln -s "${COMFYUI_MODELS_PATH:-${workspace}/comfyui/models}" "${release}/app/models"
ln -s "${COMFYUI_CUSTOM_NODES_PATH:-${workspace}/comfyui/custom_nodes}" "${release}/app/custom_nodes"

"${release}/venv/bin/python" -c \
  'import torch; assert torch.backends.mps.is_available(), "MPS unavailable"'

printf '%s\n' "${version}" > "${release}/VERSION"
printf '%s\n' "${commit}" > "${release}/COMMIT"
printf '%s\n' "${fingerprint}" > "${release}/FINGERPRINT"

ln -s "releases/${fingerprint}" "${new_link}"
old_release=""
if [[ -L "${current}" ]]; then
  old_target=$(readlink "${current}")
  case "${old_target}" in
    releases/*) old_release="${base}/${old_target}" ;;
  esac
fi
if [[ -e "${current}" || -L "${current}" ]]; then
  mv -- "${current}" "${old_link}"
fi
mv -- "${new_link}" "${current}"
installed=true

if [[ -e "${old_link}" || -L "${old_link}" ]]; then
  find "${old_link}" -depth -delete
fi
if [[ -n "${old_release}" \
      && "${old_release}" != "${release}" \
      && -d "${old_release}" ]]; then
  find "${old_release}" -depth -delete
fi

trap - EXIT INT TERM
