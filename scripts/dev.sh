#!/usr/bin/env bash
# Dev helper: backend on 7860 (+CORS for Vite) and Vite on 5173.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export RAPPORT_DEV_CORS_ORIGIN="http://127.0.0.1:5173"
export RAPPORT_PAIRING_CODE="${RAPPORT_PAIRING_CODE:-123456}"
(cd "$ROOT/backend" && uv run rapport serve) &
trap 'kill 0' EXIT
cd "$ROOT/frontend" && npm run dev
