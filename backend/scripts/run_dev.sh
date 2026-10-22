#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-0.0.0.0}"
PORT="${2:-8077}"
python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload --app-dir .

