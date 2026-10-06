# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.14 AS uv

FROM python:3.13-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN useradd --system --uid 10001 --create-home keepflame \
    && mkdir -p /app /etc/keepflame /var/lib/keepflame \
    && chown -R keepflame:keepflame /app /etc/keepflame /var/lib/keepflame

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY --chown=keepflame:keepflame pyproject.toml uv.lock ./
COPY --chown=keepflame:keepflame src ./src

RUN uv sync --frozen --no-dev --no-editable

USER keepflame
ENTRYPOINT ["keepflame"]
CMD ["run", "--config", "/etc/keepflame/config.toml"]

