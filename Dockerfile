# One image, two commands: the API (uvicorn) and the worker (tick loop).
# Built once; docker-compose picks the entrypoint per service.
FROM python:3.12-slim AS base

# Python writes no .pyc and flushes logs immediately so docker logs are live.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pinned, reproducible dependency install from uv.lock (#42): uv (from its
# published image) exports the locked versions to a requirements file, which pip
# installs; the project itself is installed --no-deps afterward. Dependencies
# live in their own layer, so a source change no longer rebuilds them — the layer
# busts only when pyproject.toml / uv.lock change.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes \
        --format requirements-txt -o /tmp/requirements.txt \
    && pip install --root-user-action=ignore -r /tmp/requirements.txt

# Project sources copied after deps. `pip install --no-deps .` builds the wheel
# via hatchling, which needs README.md (pyproject `readme = "README.md"`) and the
# package source present — so both are here before the install.
COPY README.md alembic.ini ./
COPY alembic ./alembic
COPY tgmonitor ./tgmonitor
COPY templates ./templates
COPY static ./static
RUN pip install --root-user-action=ignore --no-deps .

# Non-root user: the container shouldn't run as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command is the API; the worker overrides command in docker-compose.
CMD ["uvicorn", "tgmonitor.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
