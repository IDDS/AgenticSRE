#!/bin/sh
# Run on the connected build server with the dashboard Python environment active.
set -eu
runtime=/opt/workswarm
python -m pip install --no-cache-dir 'https://codeload.github.com/openJiuwen-ai/jiuwenswarm/tar.gz/068f148b59e140a179e31c9fe58eed8ea1700c1e#subdirectory=sdks/python'
python -m venv "$runtime"
"$runtime/bin/python" -m pip install --no-cache-dir 'workswarm==0.2.8'
HOME="$runtime" "$runtime/bin/jiuwenswarm-init" </dev/null
# Upstream loads its starter .env with override=True. Leave model settings to
# the dashboard's per-process environment (including after config rotation).
sed -i -E '/^(API_KEY|API_BASE|MODEL_NAME|MODEL_PROVIDER)=/d' "$runtime/.jiuwenswarm/config/.env"
