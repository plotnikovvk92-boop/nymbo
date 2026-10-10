#!/bin/sh
set -eu
cd /workspace/nyambo-audit/fixed
UV_CACHE_DIR=/tmp/nyambo-uv-cache uv python install 3.10.22 --no-bin --install-dir /workspace/nyambo-audit/python
/workspace/nyambo-audit/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10 -m venv /workspace/nyambo-audit/venv310
PIP_CACHE_DIR=/tmp/nyambo-pip-cache /workspace/nyambo-audit/venv310/bin/python -m pip install -r requirements-python310.lock.txt
/workspace/nyambo-audit/venv310/bin/python -m pip check
command -v pdftotext >/dev/null
