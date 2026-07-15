# One image, two commands: the API (uvicorn) and the worker (tick loop).
# Built once; docker-compose picks the entrypoint per service.
FROM python:3.12-slim AS base

# Python writes no .pyc and flushes logs immediately so docker logs are live.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies FIRST for layer caching (#35): copy only the manifest +
# lock, install deps, THEN copy the sources. A source change no longer busts
# the slow dependency-install layer. uv.lock pins resolved versions so the image
# is reproducible; if it is absent, pip falls back to pyproject lower bounds.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore .

# Sources copied after deps so they don't invalidate the dep layer.
COPY alembic.ini README.md ./
COPY alembic ./alembic
COPY tgmonitor ./tgmonitor
COPY templates ./templates
COPY static ./static

# Non-root user: the container shouldn't run as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command is the API; the worker overrides command in docker-compose.
CMD ["uvicorn", "tgmonitor.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
