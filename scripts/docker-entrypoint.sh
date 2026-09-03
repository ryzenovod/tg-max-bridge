#!/usr/bin/env sh
set -eu

if [ -n "${MAX_MCP_SESSION_TARB64:-}" ]; then
  python -m tg_max_bridge.session_seed
  unset MAX_MCP_SESSION_TARB64
fi

exec "$@"
