#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements-server.txt

# --- FunPayAPI: клонируем и ставим как локальный пакет ---
rm -rf FunPayAPI
git clone --depth 1 https://github.com/MorikTV/FunPayAPI.git FunPayAPI

# Ставим зависимости пакета (если есть requirements.txt внутри репо)
if [ -f FunPayAPI/requirements.txt ]; then
    python -m pip install -r FunPayAPI/requirements.txt
fi

# Устанавливаем сам пакет в editable-режиме, чтобы импорт FunPayAPI работал
python -m pip install -e ./FunPayAPI
