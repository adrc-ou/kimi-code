#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: install_comfy_macos.sh ROOT WORKSPACE VERSION COMMIT PYTHON INSTANCE LOCK REQUIREMENTS_SHA256" >&2
  exit 2
fi

root=$1
workspace=$2
version=$3
commit=$4
python_command=$5
instance_id=$6
lock=$7
requirements_sha256=$8
[[ "${instance_id}" =~ ^[0-9a-f]{16}$ ]] || { echo "invalid instance id" >&2; exit 2; }
[[ "${requirements_sha256}" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid requirements digest" >&2; exit 2; }
[[ -f "${lock}" ]] || { echo "no dependency lock at ${lock}" >&2; exit 2; }
# The same binding the CUDA build makes: this lock has to say it was resolved from this release's
# requirements and this commit. Installing a lock that was built for another release under
# --require-hashes does not fail, because every hash in it is correct for *its* dependency set --
# which is precisely why the claim has to be read rather than assumed.
grep -qx "# comfyui-requirements-sha256=${requirements_sha256}" "${lock}" || {
  echo "The dependency lock ${lock} was not resolved from ComfyUI ${version}'s requirements." >&2
  exit 1
}
grep -qx "# comfyui-commit=${commit}" "${lock}" || {
  echo "The dependency lock ${lock} was not resolved from commit ${commit}." >&2
  exit 1
}
base="${root}/.local/runtime/${instance_id}/module-data/comfyui/app"
releases="${base}/releases"
current="${base}/current"
old_link="${base}/old.$$"
new_link="${base}/current.$$"
installed=false
release_created=false

mkdir -p "${releases}"

# Reinstall when the application, the resolved dependency set, the backend pins, custom
# requirements, installer, or the selected host Python executable changes.
fingerprint=$(
  {
    printf '%s\n' "${version}" "${commit}" "${requirements_sha256}" "${python_command}"
    # Digest the lock's bytes rather than shasum's output for it, because the keyed path it is
    # cached under changes with the resolver version while its contents need not.
    shasum -a 256 <"${lock}"
    shasum -a 256 \
      "${root}/modules/comfyui/backend/backend.env" \
      "${root}/modules/comfyui/backend/requirements-custom.lock" \
      "${root}/modules/comfyui/scripts/install_comfy_macos.sh"
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
  --requirement "${lock}"
"${release}/venv/bin/python" -m pip install --require-hashes \
  --requirement "${root}/modules/comfyui/backend/requirements-custom.lock"

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
