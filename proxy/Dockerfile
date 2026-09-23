FROM python:3.14-alpine@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes --requirement /app/requirements.lock

COPY model_proxy.py /app/model_proxy.py

USER 65532:65532

CMD ["python", "/app/model_proxy.py"]
