# Pin the release's multi-platform index so the build target selects a native CPU image.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

# Pinning the base by digest freezes its apk set with it, and Alpine 3.24.2 predates the fix this
# carries: zlib 1.3.2-r1 for CVE-2026-85091. Naming the exact version keeps the layer as
# reproducible as the base under it, so raising it stays a deliberate edit like any other pin.
RUN apk add --no-cache zlib=1.3.2-r1

COPY app.py /app/app.py

USER 65532:65532

CMD ["python", "/app/app.py"]
