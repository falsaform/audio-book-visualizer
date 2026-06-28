# syntax=docker/dockerfile:1

# Single image used for both development (source bind-mounted via compose) and
# running the pipeline (source baked in). Package management is uv; ffmpeg is
# present for audiobook decoding (pydub / faster-whisper).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:/root/.local/bin:$PATH"

# ffmpeg: audiobook decoding. curl/ca-certificates: Claude Code installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv binary from the official distroless image (pinned for reproducibility).
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

# Claude Code CLI (native installer, no Node needed). Installs to
# /root/.local/bin/claude, which is on PATH above. Authenticate at runtime via
# the CLAUDE_CODE_OAUTH_TOKEN env var (forwarded by docker compose).
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /app

# 1) Resolve and install dependencies first, without the project itself, so this
#    layer is cached across source changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra audio --extra dev

# 2) Add the project source and install the package (editable).
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra audio --extra dev

# Whisper / huggingface model cache lives here (mounted as a volume in compose).
# Left separate from uv's cache (~/.cache/uv) so each gets its own volume.
ENV HF_HOME=/cache/huggingface

# No fixed ENTRYPOINT: the venv is on PATH, so `abv`, `pytest`, `ruff` and
# `python` are all directly runnable. The justfile drives which one to call.
CMD ["abv", "--help"]
