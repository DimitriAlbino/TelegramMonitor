# One image, two commands: the API (uvicorn) and the worker (tick loop).
# Built once; docker-compose picks the entrypoint per service.
FROM python:3.12-slim AS base

# Python writes no .pyc and flushes logs immediately so docker logs are live.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for layer caching. Install the project itself
# afterward so source changes don't bust the dependency layer.
COPY pyproject.toml alembic.ini README.md ./
COPY alembic ./alembic
COPY tgmonitor ./tgmonitor

RUN pip install --root-user-action=ignore .

# Non-root user: the container shouldn't run as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command is the API; the worker overrides command in docker-compose.
CMD ["uvicorn", "tgmonitor.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
