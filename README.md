Telegram бот для выдачи VLESS ссылок через x-ui/3x-ui
====================================================

Быстрый старт
-------------

1) Установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Настройте переменные окружения:

```bash
cp .env.example .env
# заполните .env:
# BOT_TOKEN, XUI_BASE_URL, (XUI_API_TOKEN или XUI_USERNAME/XUI_PASSWORD), XUI_INBOUND_ID
```

3) Запустите бота:

```bash
python -m bot.main
```

Использование
-------------

- Команда `/start` — краткая справка
- Команда `/vless [ГБ] [ДНЕЙ]` — создать пользователя и получить ссылку
  - по умолчанию `/vless` выдаёт 30 ГБ на 30 дней

Важно про x-ui API
------------------

Форк x-ui/3x-ui может иметь разные пути и метод авторизации. В коде реализована попытка сначала обратиться к "/panel/inbound/addClient" (JSON), затем к "/xui/inbound/addClient" (form-data). Если ваш форк использует иные пути/параметры — обновите `bot/xui_client.py` под свою панель.

Параметры подключения (host/port/SNI/flow/transport) в ссылке VLESS сейчас шаблонные (YOUR_HOST, YOUR_PORT). Подставьте реальные значения под ваш сервер и схему (REALITY, WS, gRPC и т.п.).