#!/usr/bin/env bash
# One-time developer setup: Python virtualenv + backend deps + frontend deps
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade 'pip>=26.2'
pip install -r backend/requirements.txt
(cd desktop && npm install)
echo "Done. To start:  cd desktop && npm run dev"
