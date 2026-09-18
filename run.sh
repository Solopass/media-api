#!/usr/bin/env bash
# ==============================================================================
# Workstation Media API Launcher (Pinned to E-Cores 8-15)
# Supports both Systemd Socket Activation (--fd 3) and direct CLI invocation
# ==============================================================================
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export HF_HOME="/mnt/d/AI/Cache/huggingface"
export UV_CACHE_DIR="/mnt/d/AI/Cache/uv"
export PYTHONPATH="$PROJECT_DIR"
cd "$PROJECT_DIR"

# Loopback only (workstation rule). Socket activation binds via media-api.socket;
# this applies to direct invocation. Export MEDIA_API_BIND_HOST=0.0.0.0 to reach
# the API from another machine on purpose.
BIND_HOST="${MEDIA_API_BIND_HOST:-127.0.0.1}"
BIND_PORT="${MEDIA_API_BIND_PORT:-8080}"

NOW=$(date '+%Y-%m-%d %H:%M:%S')

if [ -n "$LISTEN_FDS" ] && [ "$LISTEN_FDS" -ge 1 ]; then
    echo "[$NOW] [media-api] Systemd socket activation detected ($LISTEN_FDS fds). Pinned to E-cores 8-15 (nice -n 10)."
    exec taskset -c 8-15 nice -n 10 "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app --fd 3
else
    echo "[$NOW] [media-api] Direct invocation on http://$BIND_HOST:$BIND_PORT. Pinned to E-cores 8-15 (nice -n 10)."
    exec taskset -c 8-15 nice -n 10 "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app --host "$BIND_HOST" --port "$BIND_PORT"
fi