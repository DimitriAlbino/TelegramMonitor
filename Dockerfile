# One image, two commands: the API (uvicorn) and the worker (tick loop).
# Built once; docker-compose picks the entrypoint per service.
FROM python:3.12-slim AS base

# Python writes no .pyc and flushes logs immediately so docker logs are live.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# `pip install .` builds and installs the package via hatchling, which needs the
# manifest, the readme (pyproject sets `readme = "README.md"`), AND the package
# source present at build time — so copy all of them before installing. (An
# earlier attempt to install from the manifest alone, before copying sources,
# broke the build: hatchling aborts with "Readme file does not exist".) uv.lock
# pins resolved versions when present; the pip cache mount speeds rebuilds.
COPY pyproject.toml uv.lock* README.md alembic.ini ./
COPY alembic ./alembic
COPY tgmonitor ./tgmonitor
COPY templates ./templates
COPY static ./static
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore .

# Non-root user: the container shouldn't run as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command is the API; the worker overrides command in docker-compose.
CMD ["uvicorn", "tgmonitor.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
