#!/usr/bin/env bash
set -euo pipefail

HOST="${SKILL_RUNTIME_HOST:-0.0.0.0}"
PORT="${SKILL_RUNTIME_PORT:-8090}"

exec python -m uvicorn skill_runtime.app:app --host "${HOST}" --port "${PORT}"
