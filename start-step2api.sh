#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
PORT="${1:-8790}"
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
exec "$PY" step2api.py --port "$PORT" --data-dir accounts
