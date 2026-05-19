FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    mpv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY tuidash/ ./tuidash/
RUN uv sync --frozen

EXPOSE 8080

CMD ["tuidash", "--serve", "--port", "8080", "--host", "0.0.0.0"]
