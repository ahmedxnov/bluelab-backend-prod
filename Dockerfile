# syntax=docker/dockerfile:1
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS build

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY requirements.lock ./
RUN python -m venv .venv \
    && .venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements.lock
RUN .venv/bin/python -m playwright install chromium
COPY src ./src

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY --from=build /ms-playwright /ms-playwright
RUN .venv/bin/python -m playwright install-deps chromium \
    && adduser --disabled-password --gecos "" --home /app --uid 10001 appuser \
    && chown -R appuser:appuser /app /ms-playwright
COPY --chown=appuser:appuser --from=build /app/src /app/src

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

# Worker and sweeper deployments override this module command; all three use this
# same image digest.
CMD ["python", "-m", "bluelab.entrypoints.api"]
