FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

LABEL org.opencontainers.image.title="tg-broadcaster" \
      org.opencontainers.image.description="Self-hosted Telegram broadcasting control bot" \
      org.opencontainers.image.source="https://github.com/vaproh/tg-broadcaster" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py ./
COPY posts ./posts

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app

USER app
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app
CMD ["python", "app.py"]
