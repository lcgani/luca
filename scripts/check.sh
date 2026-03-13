#!/usr/bin/env bash
set -euo pipefail

python -m compileall backend tests
pytest -q
