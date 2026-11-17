# Pin the release's multi-platform index so the build target selects a native CPU image.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

# Install the zlib version supplied by the Alpine repositories configured
# by the pinned base image.
RUN apk add --no-cache zlib

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes --requirement /app/requirements.lock

COPY model_proxy.py /app/model_proxy.py
# The local-engine vocabulary is imported by the launcher and the engine worker as well as here, so
# it is one file shared three ways rather than a copy per consumer. Its directory is sys.path[0] at
# runtime, which is what makes a plain sibling import resolve inside the image.
COPY local_engine_contract.py /app/local_engine_contract.py

USER 65532:65532

CMD ["python", "/app/model_proxy.py"]
