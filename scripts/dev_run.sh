#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
