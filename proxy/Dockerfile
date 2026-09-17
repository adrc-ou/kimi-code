FROM python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a

# Security fix for the libuuid shipped in this immutable base image.
RUN apk add --no-cache libuuid=2.42.3-r1

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes --requirement /app/requirements.lock

COPY model_proxy.py /app/model_proxy.py

USER 65532:65532

CMD ["python", "/app/model_proxy.py"]
