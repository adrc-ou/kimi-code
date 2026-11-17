# Pin the release's multi-platform index so the build target selects a native CPU image.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

# Install the zlib version supplied by the Alpine repositories configured
# by the pinned base image.
RUN apk add --no-cache zlib

COPY app.py /app/app.py

USER 65532:65532

CMD ["python", "/app/app.py"]
