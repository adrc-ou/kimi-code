# Pin the release's multi-platform index so the build target selects a native CPU image.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

# The base digest freezes Alpine 3.24's installed package set at the moment that image was built,
# and the zlib inside it (1.3.2-r1) carries CVE-2026-4738, fixed upstream in 1.4-r0. Naming a
# version alone does not raise it, and neither does dropping the name: `apk add` is already
# satisfied by the copy in the image and installs nothing, which is why an unpinned `apk add zlib`
# left the vulnerable build exactly where it was. `--upgrade` moves that one package to the version
# named, and the version is named so the layer stays as reproducible as the base under it and the
# build fails loudly rather than silently drifting if the repository stops offering it. Verified
# present in v3.24/main today; raise it deliberately, as this repository raises any other pin.
RUN apk add --no-cache --upgrade zlib=1.4-r0

COPY app.py /app/app.py

USER 65532:65532

CMD ["python", "/app/app.py"]
