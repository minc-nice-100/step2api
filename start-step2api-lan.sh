#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
PORT="${1:-8790}"
KEY="${2:-sf-$(date +%s)-$RANDOM$RANDOM}"
PY=
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PY=$candidate
    break
  fi
done
if [ -z "$PY" ]; then
  echo "找不到可用的 Python 3（python3 / python 都无法运行）" >&2
  exit 1
fi
echo "LAN mode: 0.0.0.0:${PORT}  API Key: ${KEY}"
exec "$PY" step2api.py --host 0.0.0.0 --port "$PORT" --api-key "$KEY" --data-dir accounts
