FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG MAX_MCP_REPO=https://github.com/ryzenovod/max-mcp.git
ARG MAX_MCP_COMMIT=e15dcf39c74948b57538fa2db52ca5c30af0e504

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --no-dev --frozen

RUN git init /opt/max-mcp \
    && git -C /opt/max-mcp remote add origin "${MAX_MCP_REPO}" \
    && git -C /opt/max-mcp fetch --depth 1 origin "${MAX_MCP_COMMIT}" \
    && git -C /opt/max-mcp checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/max-mcp rev-parse HEAD)" = "${MAX_MCP_COMMIT}" \
    && uv sync --directory /opt/max-mcp --no-dev --frozen \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /home/app/.max-mcp \
    && chown -R app:app /app /home/app

ENV PATH="/app/.venv/bin:${PATH}"
ENV MAX_MCP_DIRECTORY=/opt/max-mcp
ENV HOME=/home/app
ENV UV_CACHE_DIR=/tmp/uv-cache

USER app

CMD ["tg-max-bridge", "run"]
