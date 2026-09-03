#!/usr/bin/env sh
set -eu

if [ -n "${MAX_MCP_SESSION_TARB64:-}" ]; then
  mkdir -p "${HOME}/.max-mcp"
  printf '%s' "${MAX_MCP_SESSION_TARB64}" \
    | base64 -d \
    | tar -xz -C "${HOME}/.max-mcp"
  chmod 700 "${HOME}/.max-mcp"
  find "${HOME}/.max-mcp" -type f -exec chmod 600 {} \;
fi

exec "$@"
