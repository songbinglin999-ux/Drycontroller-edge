#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export LOG_ROOT="${LOG_ROOT:-/tmp/drycontroller-logs}"
mkdir -p "$LOG_ROOT"

if [ "${CI:-}" = "true" ]; then
  python -m pytest --cov=services --cov=common --cov-report=term-missing --cov-report=xml:coverage.xml --junitxml=junit.xml "$@"
else
  PY_BIN="${PYTHON_BIN:-}"
  if [ -z "$PY_BIN" ]; then
    if command -v python3.12 >/dev/null 2>&1; then
      PY_BIN="python3.12"
    else
      PY_BIN="python3"
    fi
  fi

  VENV_DIR="${VENV_DIR:-.venv-test}"
  if [ ! -d "$VENV_DIR" ]; then
    "$PY_BIN" -m venv "$VENV_DIR"
  fi
  source "$VENV_DIR/bin/activate"

  python -m pip install --upgrade pip
  python -m pip install -r requirements-test.txt

  python -m pytest --cov=services --cov=common --cov-report=term-missing "$@"
fi
