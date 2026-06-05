#!/bin/bash

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv-py311"

cd "$PROJECT_DIR"

if [ ! -d "$VENV" ]; then
  echo "Virtual environment not found at $VENV"
  exit 1
fi

source "$VENV/bin/activate"

# Kill any process already holding the dashboard port (default 8080)
DASH_PORT=${DASH_PORT:-8080}
EXISTING_PIDS=$(lsof -ti :"$DASH_PORT" 2>/dev/null || true)
if [ -n "$EXISTING_PIDS" ]; then
  echo "Killing existing processes on port $DASH_PORT (PIDs: $(echo $EXISTING_PIDS | tr '\n' ' '))..."
  # shellcheck disable=SC2086
  kill -9 $EXISTING_PIDS 2>/dev/null || true
  # Wait until the port is actually free (up to 5s)
  for i in $(seq 1 10); do
    lsof -ti :"$DASH_PORT" >/dev/null 2>&1 || break
    sleep 0.5
  done
  # Extra pause so Angel One's server-side WebSocket connection registers the
  # disconnection before the new session tries to connect (avoids 429 burst).
  echo "Waiting 5s for Angel One to release the previous WebSocket connection..."
  sleep 5
fi

python main.py "$@"
