# Booster Platform — финальная установка

## 1. Neon PostgreSQL

1. Открой Neon → свой проект → SQL Editor.
2. Открой `migration.sql` из этого архива.
3. Скопируй весь файл в SQL Editor.
4. Нажми Run.
5. Не создавай таблицы вручную.
6. Не запускай старую `migration_funpay_chat.sql` отдельно.

## 2. GitHub

Создай отдельный репозиторий для серверной части.

В корень репозитория положи:

- `server.py`
- `requirements-server.txt`
- `render-build.sh`

`migration.sql` можно хранить в репозитории как историю схемы, но он не запускается Render автоматически.

ВАЖНО: НЕ загружай в GitHub:

- `RENDER_ENV_VALUES.txt`
- `READY_CONFIG.txt`
- любой `.env`
- `golden_key`
- пароль Neon в открытом файле

`.gitignore` уже подготовлен.

## 3. Render

Создай Web Service из этого GitHub-репозитория.

Build Command:

    bash render-build.sh

Start Command:

    uvicorn server:app --host 0.0.0.0 --port $PORT

Environment Variables:

    DATABASE_URL = значение из RENDER_ENV_VALUES.txt
    ADMIN_KEY = TEST-KEY-FOR-ME-0001
    FACEIT_API_KEY = оставить пустым
    FUNPAY_TOKEN_SECRET = значение из RENDER_ENV_VALUES.txt
    FUNPAY_DOTA_CHAT_ID = game-41
    FUNPAY_CS2_CHAT_ID = game-333

После сохранения нажми Manual Deploy → Deploy latest commit.

## 4. Проверка Render

Открой:

    https://booster-server-le7d.onrender.com/

Сервер должен ответить JSON со статусом сервиса.

Если Render падает на `FunPayAPI`, сначала смотри Build Logs: `render-build.sh` устанавливает зависимости и скачивает FunPayAPI автоматически.

## 5. Клиент

На локальном Windows-ПК установи Python и зависимости из `requirements-client.txt`.

Запуск для проверки:

    python client.py

Клиент использует сервер:

    https://booster-server-le7d.onrender.com

Не добавляй `DATABASE_URL`, `ADMIN_KEY` или `FUNPAY_TOKEN_SECRET` в `client.py`.

## 6. FunPay

Каждый бустер подключает свой собственный FunPay `golden_key` через приложение.

Общие каналы:

- Dota 2: `game-41`
- CS2: `game-333`

Личные чаты зависят от подключённого FunPay-аккаунта.

Никому не передавай `golden_key`.

## 7. Порядок первой проверки

1. Neon migration.
2. GitHub push серверных файлов.
3. Render deploy.
4. Проверка `/`.
5. Запуск нового `client.py`.
6. Подключение тестового FunPay.
7. Проверка личного чата.
8. Проверка общего Dota-чата.
9. Проверка общего CS2-чата.
10. Проверка отправки сообщения из приложения в FunPay.
11. Проверка импорта заказа.

## 8. После успешного теста

Следующий этап — сборка Windows `.exe` из `client.py`. Сервер и Neon в `.exe` не встраиваются.
