#!/bin/sh
# Reloading local launcher; install requirements-dev.txt first. Stores runs in ./experiments.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
export EXPERIMENTS_DIR="${EXPERIMENTS_DIR:-$ROOT/experiments}"

# One worker matches the local-store requirement of a single process writer.
exec gunicorn --reload --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread \
  --workers 1 --threads "${THREADS:-8}" --timeout 0 --graceful-timeout 30 flask-app:app
