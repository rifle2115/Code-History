#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
[ -f .env ] || cp .env.example .env

exec .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000
