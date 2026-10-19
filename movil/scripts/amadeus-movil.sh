#!/usr/bin/env bash
# Lanzador de Amadeus Móvil Android (R.A.M.S. BIO-CORE + VTuber AI Studio) en Arch Linux
set -euo pipefail
cd "$(dirname "$0")/../.."

if command -v adb >/dev/null 2>&1; then
    adb reverse tcp:8767 tcp:8767 >/dev/null 2>&1 || true
fi

exec ./.venv/bin/python -m movil.servidor_movil "$@"
