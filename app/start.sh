#!/usr/bin/env bash
set -e

# Load .env if present
if [ -f "$(dirname "$0")/../.env" ]; then
  export $(grep -v '^#' "$(dirname "$0")/../.env" | xargs)
fi

echo "[start] Installing Scrapling browsers..."
scrapling install 2>/dev/null || true

echo "[start] Starting Scrapling SaaS on http://0.0.0.0:8000"
cd "$(dirname "$0")"
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
