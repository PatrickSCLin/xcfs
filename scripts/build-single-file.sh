#!/bin/bash
# Build dist/xcfs: the whole tool as one executable file (a Python zip application), for installing by copying a
# single file (dotfiles managers, package managers).
#
#   scripts/build-single-file.sh [output]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
OUT="${1:-${ROOT}/dist/xcfs}"
stage=$(mktemp -d)
trap 'rm -rf "${stage}"' EXIT
cp -R "${ROOT}/lib/xcfs" "${stage}/xcfs"
find "${stage}" -name __pycache__ -type d -prune -exec rm -rf {} +
cat > "${stage}/__main__.py" <<'PY'
import sys
from xcfs.cli import main
sys.exit(main(sys.argv[1:]))
PY
mkdir -p "$(dirname "${OUT}")"
python3 -m zipapp "${stage}" -o "${OUT}" -p "/usr/bin/env python3" -c
chmod 755 "${OUT}"
echo "built ${OUT} ($("${OUT}" --version))"
