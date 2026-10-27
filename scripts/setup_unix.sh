#!/usr/bin/env bash
# Development setup on macOS / Linux:  bash scripts/setup_unix.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -e ".[dev]" --quiet
[ -f .env ] || { cp .env.example .env; echo "Created .env - add the API keys."; }
[ -f kb/answers.yaml ] || { cp kb/answers.example.yaml kb/answers.yaml; echo "Created kb/answers.yaml."; }
mkdir -p data output logs
.venv/bin/python -m jobagent doctor
echo "Run tests: .venv/bin/python -m pytest -q"
