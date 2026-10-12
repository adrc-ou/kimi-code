FROM python:3.14-alpine@sha256:7f2ec713f0436c59df2362d973b6a05af9e87ba78f0e94f5245c302984a7a1fd

# Pinning the base by digest freezes its apk set with it, and Alpine 3.24.2 predates the fix this
# carries: zlib 1.3.2-r1 for CVE-2026-85091. Naming the exact version keeps the layer as
# reproducible as the base under it, so raising it stays a deliberate edit like any other pin.
RUN apk add --no-cache zlib=1.3.2-r1

COPY app.py /app/app.py

USER 65532:65532

CMD ["python", "/app/app.py"]
