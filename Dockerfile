# Ariadne API and worker.
#
# One image serves both roles: `uvicorn` for the API, and the same image with a different
# command for a Cloud Run worker. Sharing the image means the worker can never drift from
# the code that produced the evidence the API serves.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a source change does not invalidate the dependency layer.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install --no-cache-dir ".[gcp]" || \
    pip install --no-cache-dir .

COPY backend ./backend
COPY benchmark ./benchmark

# Run as a non-root user. The Cloud Run service account carries the identity that matters,
# but a container that cannot write outside its own volume is one less thing to reason about.
RUN useradd --create-home --uid 1001 ariadne && \
    mkdir -p /app/var && chown -R ariadne:ariadne /app
USER ariadne

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/health')" || exit 1

CMD ["sh", "-c", "uvicorn backend.api.main:app --host 0.0.0.0 --port ${PORT}"]
