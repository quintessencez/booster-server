#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements-server.txt
rm -rf FunPayAPI
git clone --depth 1 https://github.com/MorikTV/FunPayAPI.git FunPayAPI
