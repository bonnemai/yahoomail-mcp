#!/bin/sh
set -eu

MODE=${1:-local}
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

find_python() {
  for candidate in "${PYTHON:-}" "$ROOT/.venv/bin/python" \
      /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
    [ -n "$candidate" ] || continue
    if [ "$candidate" != "python3" ] && [ ! -x "$candidate" ]; then
      continue
    fi
    if "$candidate" -c 'import sys, encodings; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

case "$MODE" in
  local)
    if [ -e "$ROOT/.venv" ]; then
      if [ ! -x "$ROOT/.venv/bin/python" ] || ! "$ROOT/.venv/bin/python" -c 'import sys, encodings; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        echo "The project's .venv is missing, broken, or older than Python 3.11." >&2
        echo "Repair it with a working Python 3.11+ interpreter:" >&2
        echo "  rm -rf .venv && python3.13 -m venv .venv" >&2
        echo "  .venv/bin/python -m pip install -e ." >&2
        exit 1
      fi
      PYTHON="$ROOT/.venv/bin/python"
    elif ! PYTHON=$(find_python); then
      echo "No working Python 3.11+ interpreter was found." >&2
      echo "Repair the virtual environment with:" >&2
      echo "  python3.13 -m venv .venv" >&2
      echo "  .venv/bin/python -m pip install -e ." >&2
      exit 1
    fi
    if [ -f "$ROOT/.env" ]; then
      set -a
      . "$ROOT/.env"
      set +a
    fi
    export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
    exec "$PYTHON" -m yahoomail_mcp.server
    ;;
  docker)
    exec docker compose -f "$ROOT/compose.yaml" up --build
    ;;
  macos-linux|container)
    command -v container >/dev/null 2>&1 || {
      echo "Apple Container CLI is required for macos-linux mode." >&2
      exit 1
    }
    container system start >/dev/null 2>&1 || true
    container build --tag yahoo-mail-mcp:local "$ROOT"
    container rm --force yahoo-mail-mcp >/dev/null 2>&1 || true
    exec container run --name yahoo-mail-mcp --env-file "$ROOT/.env" \
      --publish 127.0.0.1:8000:8000 yahoo-mail-mcp:local
    ;;
  *)
    echo "Usage: $0 [local|docker|macos-linux]" >&2
    exit 2
    ;;
esac
