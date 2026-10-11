# Veritas Bot

Собственный бот закрытого сервера «Veritas»: настройка структуры, верификация, тикеты, логи,
защита от рейдов и сноса, иерархия и наказания, мост с сайтом.

> README дополняется по мере готовности фаз (см. `PROMPT.md`, §17).

## Быстрый старт (разработка)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # заполни DISCORD_TOKEN и GUILD_ID
python -m pytest -q
ruff check .
python -m veritas      # запуск бота
```
