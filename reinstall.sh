#!/usr/bin/env bash
# Clean, idempotent editable reinstall. Safe to run anytime.
# Use this after pulling a change that modifies pyproject.toml or the package layout.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    echo "ERROR: activate your virtualenv first (source .venv/bin/activate)" >&2
    exit 1
fi

echo "==> Removing stale build artifacts"
rm -rf build dist *.egg-info worldreveal.egg-info

echo "==> Uninstalling any previous worldreveal install"
pip uninstall -y worldreveal-downloader worldreveal 2>/dev/null || true

echo "==> Installing in editable mode"
pip install -e .

echo "==> Verifying"
python -c "import worldreveal; print('  worldreveal package:', worldreveal.__file__)"
which worldreveal
echo
echo "Done. Run 'worldreveal' to start the monitor."
