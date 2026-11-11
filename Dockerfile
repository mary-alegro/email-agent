# syntax=docker/dockerfile:1
#
# Supply Chain Email Agent
#
#   docker build -t supply-chain-email-agent .                        # service image
#   docker build --target test -t supply-chain-email-agent:test .     # test image
#   docker run --rm supply-chain-email-agent:test                     # runs the test suite
#   (make docker-test does the last two)
#
# The service image runs as a non-root user, stores its SQLite database on the /data
# volume, seeds the mock ERP on start (idempotent), and serves on port 8000.

ARG PYTHON_VERSION=3.12

# --- deps: locked runtime dependencies in /app/.venv -------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Only the lockfile and pyproject: this layer is cached until dependencies change.
# The project itself isn't installed; it runs from source in /app.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

# --- test: dev dependencies + tests; running the image runs the suite -------------
# Tests run at container run time (not as a build step), so their output is always shown
# and Docker's build cache can never skip them. A failing test fails `docker run`.
FROM deps AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --extra dev
COPY pyproject.toml ./
COPY app ./app
COPY src ./src
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest", "-v"]

# --- runtime: slim image with the venv and the source only -------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY app ./app
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/app.db \
    WEB_CONCURRENCY=2
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
# Seed the mock ERP (safe to repeat), then hand over to the command (uvicorn by default).
# uvicorn takes its worker count from WEB_CONCURRENCY.
ENTRYPOINT ["sh", "-c", "python -m src.database.seed && exec \"$@\"", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
