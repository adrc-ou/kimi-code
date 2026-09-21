# syntax=docker/dockerfile:1

# The same locked base the CUDA backend uses, so this container adds no new digest to certify.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
FROM ${PYTHON_IMAGE}

# The forwarder is stdlib-only: no apt packages and no pip requirements, so nothing here needs a
# hash-checked lock, a compatibility entry, or a recertification when the backend moves.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY modules/comfyui/scripts/comfy_ui_forwarder.py /app/comfy_ui_forwarder.py

# Overridden by the overlay's `user:`; this keeps a hand-run container from starting as root.
USER 1000:1000
WORKDIR /app

# The listener address and the certificate path come from the overlay. Both defaults are the
# fail-closed pair: loopback, and no authority to verify the bridge with.
ENTRYPOINT ["python", "/app/comfy_ui_forwarder.py"]
