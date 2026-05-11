#!/usr/bin/env bash
# Download React + Babel UMD bundles into static/vendor/ for offline / air-gapped installs.
# After running, update script src URLs in static/index.html to match (see README).

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/static/vendor"
mkdir -p "${OUT}"

curl -fsSL -o "${OUT}/react.production.min.js" \
  "https://unpkg.com/react@18.3.1/umd/react.production.min.js"
curl -fsSL -o "${OUT}/react-dom.production.min.js" \
  "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"
curl -fsSL -o "${OUT}/babel.min.js" \
  "https://unpkg.com/@babel/standalone@7.26.10/babel.min.js"

echo "Vendored scripts written to ${OUT}"
echo "Next: point <script src=...> in static/index.html to /static/vendor/..."
