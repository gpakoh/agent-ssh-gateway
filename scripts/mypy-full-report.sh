#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
python -m mypy app --show-error-codes --pretty || true
