#!/usr/bin/env bash
# Expose local FastAPI (:8000) to the public internet so TradingView alerts can reach /tv/webhook.
# Requires `ngrok` (https://ngrok.com) installed and authenticated.
set -euo pipefail
PORT="${PORT:-8000}"
exec ngrok http "$PORT"
