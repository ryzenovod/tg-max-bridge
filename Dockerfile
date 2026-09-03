FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

ARG MAX_MCP_REPO=https://github.com/ryzenovod/max-mcp.git
ARG MAX_MCP_COMMIT=e90d80278f9ae644d22bddc4d34697ecbd508a55

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN uv sync --no-dev --frozen

RUN git init /opt/max-mcp \
    && git -C /opt/max-mcp remote add origin "${MAX_MCP_REPO}" \
    && git -C /opt/max-mcp fetch --depth 1 origin "${MAX_MCP_COMMIT}" \
    && git -C /opt/max-mcp checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/max-mcp rev-parse HEAD)" = "${MAX_MCP_COMMIT}" \
    && uv sync --directory /opt/max-mcp --no-dev --frozen \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /home/app/.max-mcp \
    && chmod +x /app/scripts/docker-entrypoint.sh \
    && chown -R app:app /app /home/app

ENV PATH="/app/.venv/bin:${PATH}"
ENV MAX_MCP_DIRECTORY=/opt/max-mcp
ENV HOME=/home/app
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV PORT=8080

EXPOSE 8080

USER app

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["tg-max-bridge", "run"]
