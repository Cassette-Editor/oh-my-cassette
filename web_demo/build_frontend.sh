#!/usr/bin/env bash
# Build the web demo frontend (Vite + React) into web_demo/frontend/dist,
# which web_demo/server.py serves under /static.
#
# Requires Node.js + npm at build time only (not at runtime).
# Run this once before starting `python -m web_demo.server`, and again after
# pulling changes under web_demo/frontend.
set -euo pipefail

cd "$(dirname "$0")/frontend"

npm install
npm run build

echo "Built web_demo/frontend/dist"
