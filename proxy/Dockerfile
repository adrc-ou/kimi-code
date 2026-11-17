# Pin the release's multi-platform index so the build target selects a native CPU image.
# This index is python:3.14.10-alpine. The pin before it was a 3.14.8-era index that carried
# CVE-2026-115557 (Critical, the ssl module's compromised-certificate denylist) and CVE-2026-107891
# (High, musl regcomp integer overflow) - neither of which any amount of scanning the floating
# `3.14-alpine` tag would have shown, because the tag had moved ahead of the pin weeks earlier.
# So scan the digest, never the tag: `python3 scripts/check_vulnerabilities.py` reads the digests
# out of these files and gates on them. Raise deliberately, and re-scan the new digest in the same
# change; `3.14.11` on 2026-12-01 will be the next reason to.
FROM python:3.14-alpine@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67

# The base digest freezes Alpine 3.24's installed package set at the moment that image was built,
# and the zlib inside it (1.3.2-r1) carries CVE-2026-4738, fixed upstream in 1.4-r0. Naming a
# version alone does not raise it, and neither does dropping the name: `apk add` is already
# satisfied by the copy in the image and installs nothing, which is why an unpinned `apk add zlib`
# left the vulnerable build exactly where it was. `--upgrade` moves that one package to the version
# named, and the version is named so the layer stays as reproducible as the base under it and the
# build fails loudly rather than silently drifting if the repository stops offering it. Verified
# present in v3.24/main today; raise it deliberately, as this repository raises any other pin.
RUN apk add --no-cache --upgrade zlib=1.4-r0

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes --requirement /app/requirements.lock

COPY model_proxy.py /app/model_proxy.py
# The local-engine vocabulary is imported by the launcher and the engine worker as well as here, so
# it is one file shared three ways rather than a copy per consumer. Its directory is sys.path[0] at
# runtime, which is what makes a plain sibling import resolve inside the image.
COPY local_engine_contract.py /app/local_engine_contract.py

USER 65532:65532

CMD ["python", "/app/model_proxy.py"]
