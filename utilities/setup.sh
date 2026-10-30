#!/usr/bin/env bash
# Create or refresh the utilities virtual environment (utilities/.venv).
#
#   ./utilities/setup.sh                 # use python3; also installs the OCR packages
#   ./utilities/setup.sh --no-ocr        # skip them (about 160 MB); picture tables stay pictures
#   PYTHON=python3.13 ./utilities/setup.sh
#
# On Windows without bash, run the pip commands below by hand inside utilities\\.venv.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-python3}"
ocr=1
for arg in "$@"; do
    case "$arg" in
        --no-ocr) ocr=0 ;;
        *) echo "usage: setup.sh [--no-ocr]" >&2; exit 2 ;;
    esac
done

if [ ! -d "$here/.venv" ]; then
    "$python" -m venv "$here/.venv"
fi

"$here/.venv/bin/python" -m pip install --upgrade pip
"$here/.venv/bin/python" -m pip install -r "$here/requirements.txt"
if [ "$ocr" = 1 ]; then
    "$here/.venv/bin/python" -m pip install -r "$here/requirements-ocr.txt"
fi
