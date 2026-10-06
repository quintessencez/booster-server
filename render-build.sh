#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements-server.txt

# Устанавливаем FunPayAPI из PyPI (или с GitHub напрямую через pip)
python -m pip install FunPayAPI
