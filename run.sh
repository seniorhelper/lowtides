#!/usr/bin/env sh
# Start CostCop on http://localhost:8787
cd "$(dirname "$0")"
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" -m costcop.server
