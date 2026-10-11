# Veritas Bot

Собственный бот закрытого сервера «Veritas». Заменяет Carl-bot, Ticket Tool, Logger и Wick/Sapphire
и знает иерархию ордена:

| Модуль | Что делает | Команды |
|---|---|---|
| Настройка | роли, права, 7 категорий, 16 каналов, закреплённые тексты — идемпотентно | `/setup run`, `/setup dry-run`, `/panel`, `/status`, `/backup` |
| Верификация | роль Незнакомца при входе, анкета, решение Доверенных | кнопка в `#верификация`, кнопки в `#анкеты` |
| Тикеты | допуск / жалоба / вопрос Совету, транскрипты | кнопки в `#заявки`, `/ticket add/remove/close/rename` |
| Приём материалов | кнопка «Отправить материал» → приватный тикет, проверяют Глава Совета и Основатель | `/mainez` — опубликовать панель в канале |
| Логи | вход/выход, роли, сообщения, сервер, голос, события бота | `/logs test/toggle/status` |
| Защита | анти-рейд с локдауном, анти-спам, anti-nuke, защита прав | `/lockdown on/off/status`, `/raid config` |
| Иерархия | повышения, Лжец, Отлучённый, тайм-ауты, предупреждения | `/rank promote/demote/set/info`, `/liar`, `/exile`, `/restore`, `/timeout`, `/untimeout`, `/warn`, `/warns` |
| Архив и сайт | объявления в `#архив`/`#хранилище`, HTTP API допуска | `/archive announce`, API — см. [docs/API.md](docs/API.md) |

Источник истины по структуре — `veritas/config.py` (перенесено из эталонного `veritas_server.py`,
который лежит рядом для сверки). Принятые решения — [docs/DECISIONS.md](docs/DECISIONS.md),
сверка с ТЗ — [docs/CHECKLIST.md](docs/CHECKLIST.md).

---

## 1. Установка с нуля (пошагово)

### 1.1. Создать бота

1. Открой <https://discord.com/developers/applications> → **New Application** → назови «Veritas».
2. Слева **Bot** → **Reset Token** → скопируй токен. Это пароль бота: никому не показывай,
   не публикуй, не коммить.
3. Там же, **Privileged Gateway Intents**: включи
   - **Server Members Intent** — обязательно (без него бот не запустится);
   - **Message Content Intent** — нужен анти-спаму и содержимому сообщений в логах. Если выключить,
     бот запустится, предупредит в консоли и отключит эти функции.
4. Слева **General Information** → скопируй **Application ID**.

### 1.2. Пригласить бота

Подставь свой Application ID вместо `APP_ID`.

**Для первичной настройки** (с «Администратором» — проще всего):

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=8
```

**Штатный режим** (минимальный набор без «Администратора»): Manage Roles, Manage Channels,
Kick Members, Ban Members, Moderate Members, View Audit Log, Manage Messages, Pin Messages,
Manage Threads, View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files,
Read Message History, Add Reactions:

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=2253191651650774
```

То же плюс **Manage Server** (нужно только анти-рейду, чтобы ставить приглашения на паузу и поднимать
уровень проверки, и логам, чтобы видеть, кто кого пригласил):

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=2253191651650806
```

### 1.3. Поднять роль бота

Discord разрешает менять только роли **ниже** своей. Владелец сервера: Настройки сервера → Роли →
перетащи роль бота.

- **Для первой настройки** — на самый верх (чтобы бот мог выставить права всем ролям).
- **В штатном режиме — выше «⚖️ Член Совета», но ниже «👑 Глава Совета».** Зачем: если токен бота
  когда-нибудь утечёт, злоумышленник не сможет через бота выдать себе роль Главы или Основателя и
  не сможет тронуть их самих. Бот при этом продолжает управлять всеми ролями ниже — Совет и младше.

### 1.4. Заполнить `.env`

```bash
cp .env.example .env
```

Минимум: `DISCORD_TOKEN` и `GUILD_ID` (Discord → Настройки → Расширенные → Режим разработчика;
потом ПКМ по серверу → «Копировать ID сервера»). Остальное описано в `.env.example`.

ID ролей уже прописаны в `veritas/config.py` (`ROLE_IDS`). «Основатель» и «Последователь» ищутся по
названию; Последователя бот создаст сам, если не найдёт.

### 1.5. Запуск без Docker

Нужен Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m veritas setup --dry-run   # показать план: что будет создано/обновлено
python -m veritas setup             # применить
python -m veritas                   # запустить бота
```

`setup` можно запускать сколько угодно раз: существующее обновляется, ничего не дублируется и **ничего
не удаляется**. То же доступно из Discord: `/setup dry-run` и `/setup run` (только Основатель и Глава).

### 1.6. Запуск в Docker (рекомендуется для сервера)

```bash
mkdir -p data && sudo chown 1000:1000 data   # бот в контейнере работает не от root
docker compose run --rm bot python -m veritas setup --dry-run
docker compose run --rm bot python -m veritas setup
docker compose up -d --build
docker compose logs -f bot
```

База лежит в `./data/veritas.db` (том), контейнер перезапускается сам (`restart: unless-stopped`).

### 1.7. После первой настройки

1. Проверь `/status` и `/logs test`.
2. Сбрось токен (Developer Portal → Bot → Reset Token), если он где-то светился, и обнови `.env`.
3. Понизь бота до штатных прав (ссылка выше — переприглашать не нужно, достаточно снять с роли
   бота «Администратор» и выдать перечисленные права) и опусти его роль под Главу Совета.

---

## 2. Хостинг

| Вариант | Для чего | Заметки |
|---|---|---|
| **Свой VPS + Docker** | основной | 1 vCPU / 512 МБ хватает. `docker compose up -d`. Бэкапы — `python -m veritas backup` по cron. |
| **Render (бесплатно)** | без своего сервера | Готово: `render.yaml`, само-пинг, копия базы в Discord. Пошагово — [docs/RENDER.md](docs/RENDER.md). |
| **PaaS (Railway, Fly.io)** | если не хочется админить | Нужен постоянный том для `data/` (SQLite). Fly: `fly volumes create`; Railway: Volume, смонтированный в `/app/data`. Переменные окружения — в панели, не в репозитории. |
| **Домашний ПК** | только для теста | Бот офлайн, когда ПК выключен; защита сервера в это время не работает. |

Резервная копия базы: `python -m veritas backup` → `data/backups/veritas-ГГГГММДД-ЧЧММСС.db`
(безопасно на работающем боте). В Docker: `docker compose exec bot python -m veritas backup`.

---

## 3. Мост с сайтом (HTTP API)

Включается, если задан `API_TOKEN` (≥ 16 символов):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

и `ALLOWED_SITE_DOMAINS=veritas.example.com`. API слушает `127.0.0.1:8080` — наружу его выставляет
reverse-proxy с HTTPS. Методы и примеры `curl` — в [docs/API.md](docs/API.md).

**Caddy** (сам получит сертификат):

```
api.veritas.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**nginx** (сертификат — через certbot):

```nginx
server {
    listen 443 ssl;
    server_name api.veritas.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.veritas.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.veritas.example.com/privkey.pem;

    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Токен держи только на сервере сайта (в его переменных окружения), не в браузерном JavaScript:
сайт ходит в API со своего бэкенда.

---

## 4. Как это устроено

```
veritas/
├── config.py          роли, допуски, 16 каналов, пороги — единый источник правды
├── permissions.py     чистый расчёт overwrites + симулятор прав Discord
├── clearance.py       допуск 0..5 и правила «кто кого может»
├── setup_runner.py    идемпотентная настройка (план → применение)
├── db.py              SQLite (aiosqlite) + миграции migrations/NNN_*.sql
├── logservice.py      очередь логов: батчи, ретраи при 429
├── audit.py           кэш журнала аудита («кто сделал»)
├── logic/             чистые правила: анкеты, тикеты, логи, рейды/спам/nuke, активность
├── cogs/              модули бота: setup, verification, tickets, logs, antiraid, ranks, archive
├── api/               FastAPI для сайта
└── content/           тексты: правила, «о секте», фразы бота
```

Права и роли:

- «Администратор», «Управление сервером», вебхуки — только у Основателя; каналы — только у Главы
  (`ROLE_PERMS`). Если «Администратор» появится у другой роли, бот предупредит в консоли, в `#логи`
  и `#совет` (с кнопкой отката), но сам не снимет.
- Матрица видимости 9 персонажей × 16 каналов зафиксирована в `tests/test_permissions.py`.

---

## 5. Разработка

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

Тесты не ходят в Discord: Discord-объекты подменяются (`tests/fakes.py`), база — настоящий SQLite
во временной папке. CI (GitHub Actions) запускает `ruff` и `pytest` на каждый push.

---

## 6. Что нужно сделать человеку

Бот этого сделать не может — только владелец сервера:

1. Создать приложение и бота, получить токен (§1.1).
2. Включить **Server Members Intent** и **Message Content Intent** (§1.1).
3. Пригласить бота и поднять его роль (§1.2–1.3).
4. Включить режим «Сообщество» (Настройки сервера → Включить сообщество), чтобы `📢┃новости` стал
   каналом объявлений; затем снова `/setup run`.
5. Выдать людям роли (кроме автоматических: Незнакомец при входе, Последователь по анкете).
6. Удалить старые лишние каналы вручную — бот ничего не удаляет (кроме каналов своих тикетов).
7. После первой настройки: сбросить токен, если он светился; понизить права бота до штатных (§1.7).
8. Поднять сайт, HTTPS и reverse-proxy для API (§3).
9. Проверить на **тестовом** сервере: ритуал входа (анкета → принять/отклонить), тикет (открыть →
   взять → закрыть, транскрипт), антирейд (`/raid config raid.join_threshold 3`, зайти тремя
   аккаунтами, потом `/lockdown off` и вернуть порог), `/liar` → `/restore`.
   Бот покрыт тестами, но на живом Discord его проверить можешь только ты.
