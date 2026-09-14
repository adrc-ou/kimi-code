#!/usr/bin/env bash
set -euo pipefail

[[ $# -ge 2 ]] || { echo "usage: generate_sbom.sh OUTPUT_DIR IMAGE..." >&2; exit 2; }
output=$1
shift
mkdir -p "${output}"
for image in "$@"; do
  safe_name=${image//[^a-zA-Z0-9_.-]/_}
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    anchore/syft:v1.33.0@sha256:f94e5d9fce1f2278491a8e3a63bd5f6ddb81fdfdbb8bf7a1637565c1d5344357 \
    "${image}" --output cyclonedx-json >"${output}/${safe_name}.cdx.json"
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    anchore/grype:v0.99.1@sha256:aeaded256cc61f162774b1c0e19f44e1787d0ae7cdc8e8917e9ea2528375090e \
    "${image}" --fail-on high --only-fixed
done
