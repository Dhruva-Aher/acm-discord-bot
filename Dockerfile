# Multi-stage build: dependencies are resolved once in the builder and the
# runtime image ships only the virtualenv and the source, with no compiler
# toolchain and no build cache.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the dependency resolution needs first, so a source-only change
# does not invalidate the cached dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=text

# The bot never needs root: it opens outbound connections only.
RUN useradd --create-home --uid 10001 acmhub

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

USER acmhub

# Liveness for orchestrators that cannot use a port: the process is healthy if
# it can still import its own configuration machinery and reach the database.
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD ["python", "-m", "acm_hub.healthcheck"]

# SIGTERM must reach Python directly for the graceful shutdown path to run,
# so this is exec form with no shell wrapper.
CMD ["python", "-m", "acm_hub.main"]
