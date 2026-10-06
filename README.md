# Booster Platform — готовый FunPay/Flet комплект

## Что уже заполнено
- DATABASE_URL: готов
- ADMIN_KEY: TEST-KEY-FOR-ME-0001
- FACEIT: отключён
- FUNPAY_TOKEN_SECRET: сгенерирован
- FunPay Dota public chat: game-41
- FunPay CS2 public chat: game-333
- Server URL: https://booster-server-le7d.onrender.com

## 1. Neon
Откройте Neon → SQL Editor. Запустите целиком `migration.sql`.
Миграция рассчитана на повторный запуск.

## 2. Render
В репозитории сервера замените `server.py`, `requirements-server.txt`, `render-build.sh` на файлы из этой папки.

Build Command:
`bash render-build.sh`

Start Command:
`uvicorn server:app --host 0.0.0.0 --port $PORT`

Environment Variables:
`DATABASE_URL` — значение из `.env.example`
`ADMIN_KEY` — `TEST-KEY-FOR-ME-0001`
`FACEIT_API_KEY` — не добавлять или оставить пустым
`FUNPAY_TOKEN_SECRET` — значение из `.env.example`
`FUNPAY_DOTA_CHAT_ID` — `game-41`
`FUNPAY_CS2_CHAT_ID` — `game-333`

## 3. ВАЖНО про секреты
`.env.example` в этой готовой папке заполнен по запросу пользователя и содержит пароль PostgreSQL. Не загружайте этот файл в публичный GitHub.
После переноса значений в Render удалите локальную копию или храните её только в безопасном месте.

## 4. Клиент
На Windows замените старый `client.py` новым и установите:
`python -m pip install -r requirements-client.txt`

Запуск:
`python client.py`

## 5. FunPay
Каждый бустер подключает свой личный FunPay через собственный `golden_key` внутри приложения. `golden_key` не нужно вписывать в этот архив, Render или общий `.env`.

Вкладка `🟡 FUNPAY` содержит:
- личные чаты клиентов;
- общий Dota 2 чат (`game-41`);
- общий CS2 чат (`game-333`);
- чтение истории;
- отправку сообщений обратно в FunPay.

## 6. Проверка
После Render Deploy откройте:
`https://booster-server-le7d.onrender.com/`

Затем запустите клиент и проверьте подключение FunPay.

## 7. Завтра
После успешной проверки делаем Windows `.exe` из `client.py`. Сервер и Neon остаются на Render/Neon.


## New in this build
- FunPay public seller profiles, reviews and filtered Dota 2/CS2 Boost lots.
- Clickable author names in FunPay chats.
- Dota calculator dynamically uses the linked seller's matching FunPay Boost lot price for the selected current MMR, with fallback pricing.
- Additional conditions: Guarantee, Night boost, Game report.
See `инструкция.txt`.
