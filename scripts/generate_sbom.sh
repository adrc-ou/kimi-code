#!/usr/bin/env bash
set -euo pipefail

[[ $# -ge 2 ]] || { echo "usage: generate_sbom.sh OUTPUT_DIR IMAGE..." >&2; exit 2; }
output=$1
shift
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${output}"

# The recorded exceptions have to reach the scanner or the file is a list nobody honours: this is the
# gate that decides whether a finding is accepted, and scripts/check_locks.py already refuses an
# entry once its date passes, so an accepted risk fails CI on schedule instead of quietly forever.
# Only `vulnerability` plus `package.name` is matched, which is how narrow the file's own schema is.
ignore_file=$(mktemp)
trap 'rm -f "${ignore_file}"' EXIT
python3 - "${root}/vulnerability-exceptions.json" "${ignore_file}" <<'PY'
import json
import sys

source, target = sys.argv[1], sys.argv[2]
with open(source, encoding="utf-8") as handle:
    entries = json.load(handle)["exceptions"]
with open(target, "w", encoding="utf-8") as handle:
    if not entries:
        handle.write("ignore: []\n")
    else:
        handle.write("ignore:\n")
        for entry in entries:
            handle.write(f"  - vulnerability: {entry['vulnerability']}\n")
            handle.write(f"    package:\n      name: {entry['package']}\n")
            handle.write(f"    # accepted by {entry['owner']} until {entry['expires']}\n")
print(f"grype will ignore {len(entries)} recorded exception(s)")
PY
# The scan container reads this as root, but a 0600 file owned by the runner would still be an
# accident waiting for a distroless scanner image, so state the intent.
chmod 644 "${ignore_file}"

for image in "$@"; do
  safe_name=${image//[^a-zA-Z0-9_.-]/_}
  status=0
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    anchore/syft:v1.33.0@sha256:f94e5d9fce1f2278491a8e3a63bd5f6ddb81fdfdbb8bf7a1637565c1d5344357 \
    "${image}" --output cyclonedx-json >"${output}/${safe_name}.cdx.json" || status=$?
  if [ "${status}" -ne 0 ]; then
    printf '::error::syft produced no SBOM for %s (exit %s): nothing was assessed\n' \
      "${image}" "${status}"
    exit "${status}"
  fi

  # Grype's verdict and grype's breakdown are different facts, and before this they looked identical:
  # measured against the pinned v0.99.1, exit 2 means --fail-on was met - a finding someone owns -
  # while 1 means the scan itself failed and learned nothing. Both used to arrive as
  # "Process completed with exit code 2" with no way to tell the two apart from outside the log.
  status=0
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "${ignore_file}:/scan/grype-ignore.yaml:ro" \
    anchore/grype:v0.99.1@sha256:aeaded256cc61f162774b1c0e19f44e1787d0ae7cdc8e8917e9ea2528375090e \
    "${image}" --config /scan/grype-ignore.yaml --fail-on high --only-fixed || status=$?
  case "${status}" in
    0) printf 'grype found no unaccepted High or Critical fixable vulnerability in %s\n' "${image}" ;;
    2)
      printf '::error::grype found a High or Critical fixable vulnerability in %s that this repository has not accepted. Raise the base image or the package, or record a dated entry in vulnerability-exceptions.json with an owner and a reason. This job is failing because the gate held, not because it broke.\n' "${image}"
      exit 2
      ;;
    *)
      printf '::error::grype could not scan %s (exit %s): a scanner or daemon failure is not a verdict about this image\n' "${image}" "${status}"
      exit "${status}"
      ;;
  esac
done
