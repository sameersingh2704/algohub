#!/bin/bash

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv-py311"

cd "$PROJECT_DIR"

# On macOS/Linux: use the existing venv.
# On Windows Git Bash: bin/python is a macOS binary — use a separate native venv.
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]] || [[ "$(uname -s)" == CYGWIN* ]]; then
  VENV="$PROJECT_DIR/.venv-win"
  if [ ! -f "$VENV/Scripts/python.exe" ]; then
    echo "[setup] Creating Windows virtual environment..."
    py -m venv "$VENV"
    echo "[setup] Installing dependencies..."
    "$VENV/Scripts/python.exe" -m pip install --upgrade pip --quiet
    "$VENV/Scripts/python.exe" -m pip install -r "$PROJECT_DIR/requirements.txt"
    echo "[setup] Done."
  fi
  PYTHON="$VENV/Scripts/python.exe"
else
  if [ ! -d "$VENV" ]; then
    echo "Virtual environment not found at $VENV"
    exit 1
  fi
  source "$VENV/bin/activate"
  PYTHON=python
fi

# Kill any process already holding the dashboard port (default 8080)
DASH_PORT=${DASH_PORT:-8080}

if command -v lsof &>/dev/null; then
  # Linux / macOS
  EXISTING_PIDS=$(lsof -ti :"$DASH_PORT" 2>/dev/null || true)
  if [ -n "$EXISTING_PIDS" ]; then
    echo "Killing existing processes on port $DASH_PORT (PIDs: $(echo $EXISTING_PIDS | tr '\n' ' '))..."
    # shellcheck disable=SC2086
    kill -9 $EXISTING_PIDS 2>/dev/null || true
    for i in $(seq 1 10); do
      lsof -ti :"$DASH_PORT" >/dev/null 2>&1 || break
      sleep 0.5
    done
    echo "Waiting 5s for Angel One to release the previous WebSocket connection..."
    sleep 5
  fi
else
  # Windows Git Bash — lsof not available, use netstat + taskkill
  EXISTING_PIDS=$(netstat -ano 2>/dev/null | grep ":${DASH_PORT}[[:space:]]" | grep "LISTENING" | awk '{print $5}' | sort -u || true)
  if [ -n "$EXISTING_PIDS" ]; then
    echo "Killing existing processes on port $DASH_PORT..."
    for PID in $EXISTING_PIDS; do
      taskkill //F //PID "$PID" 2>/dev/null || true
    done
    echo "Waiting 5s for Angel One to release the previous WebSocket connection..."
    sleep 5
  fi
fi

$PYTHON main.py "$@"
