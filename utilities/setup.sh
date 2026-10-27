#!/usr/bin/env bash
# Create or refresh the utilities virtual environment (utilities/.venv).
#
#   ./utilities/setup.sh                 # use python3
#   PYTHON=python3.13 ./utilities/setup.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-python3}"

if [ ! -d "$here/.venv" ]; then
    "$python" -m venv "$here/.venv"
fi

"$here/.venv/bin/python" -m pip install --upgrade pip
"$here/.venv/bin/python" -m pip install -r "$here/requirements.txt"
