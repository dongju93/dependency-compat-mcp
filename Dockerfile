# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.14.7-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.7-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --create-home --uid 10001 app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app

# Cloud Run injects PORT and the identity that moves the bind address to 0.0.0.0. Run
# elsewhere and the server keeps its loopback default, so a local run needs `--host 0.0.0.0`.
EXPOSE 8080

ENTRYPOINT ["dependency-compat-mcp"]
CMD ["--transport", "http"]
