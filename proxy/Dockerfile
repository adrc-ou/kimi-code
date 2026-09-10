FROM python:3.13-alpine

RUN pip install --no-cache-dir aiohttp==3.14.3

COPY nrp_proxy.py /app/nrp_proxy.py

USER 65532:65532

CMD ["python", "/app/nrp_proxy.py"]
