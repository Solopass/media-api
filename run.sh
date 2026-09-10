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

NOW=$(date '+%Y-%m-%d %H:%M:%S')

if [ -n "$LISTEN_FDS" ] && [ "$LISTEN_FDS" -ge 1 ]; then
    echo "[$NOW] [media-api] Systemd socket activation detected ($LISTEN_FDS fds). Pinned to E-cores 8-15 (nice -n 10)."
    exec taskset -c 8-15 nice -n 10 "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app --fd 3
else
    echo "[$NOW] [media-api] Direct invocation on http://0.0.0.0:8080. Pinned to E-cores 8-15 (nice -n 10)."
    exec taskset -c 8-15 nice -n 10 "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8080
fi