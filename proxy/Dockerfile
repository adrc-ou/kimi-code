FROM python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes --requirement /app/requirements.lock

COPY nrp_proxy.py /app/nrp_proxy.py

USER 65532:65532

CMD ["python", "/app/nrp_proxy.py"]
