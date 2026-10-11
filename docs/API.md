# Veritas API (мост с сайтом)

API запускается вместе с ботом, если задан `API_TOKEN` (не короче 16 символов).
По умолчанию слушает `127.0.0.1:8080` — наружу его выставляет ваш reverse-proxy с HTTPS
(см. README, раздел «Мост с сайтом»). Документация Swagger отключена намеренно.

## Авторизация

Каждый запрос (кроме `/api/v1/health`) — с заголовком:

```
Authorization: Bearer <API_TOKEN>
```

Токен сравнивается за постоянное время (`hmac.compare_digest`). Неверный или отсутствующий
токен → `401`. Лимит — 60 запросов в минуту с одного адреса → `429`.

Сгенерировать токен: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Ответы об ошибках

| Код | Когда |
|---|---|
| 401 | нет токена или он неверный |
| 422 | неверные данные (поле вне диапазона, лишнее поле, ссылка не на разрешённый домен, похоже на пароль) |
| 429 | превышен лимит запросов |
| 503 | бот ещё не подключился к Discord или нужный канал не найден (`/setup run`) |

Тело ошибки: `{"detail": "текст"}`.

---

## `GET /api/v1/health`

Без токена. `{"ok": true, "version": "0.1.0"}` — для проверки, что процесс жив.

## `GET /api/v1/clearance/{discord_id}`

Уровень допуска человека. Использует ту же функцию, что и бот (`veritas/clearance.py`).

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" \
  http://127.0.0.1:8080/api/v1/clearance/123456789012345678
```

```json
{
  "discord_id": "123456789012345678",
  "member": true,
  "level": 2,
  "roles": ["follower", "liar"],
  "is_liar": true,
  "is_exiled": false
}
```

- `discord_id` — **строкой**: Discord ID больше, чем точно представимо числом в JavaScript.
- `level` — 0…5. Отлучённый → 0; Лжец → не выше 2.
- `roles` — машинные имена: `founder`, `head`, `council`, `archivist`, `trusted`, `initiate`,
  `follower`, `stranger`, `liar`, `exiled` (сверху вниз).
- Человека нет на сервере → `member: false`, `level: 0`, `roles: []`; `is_liar`/`is_exiled` берутся
  из базы бота (метки сохраняются после выхода).

**Вход через Discord OAuth (на будущее).** Сайт делает OAuth2 с областью `identify`, получает
Discord ID пользователя и вызывает этот метод. Если `level` ≥ грифа материала — показывать.
Отдельные «коды допуска» тогда не нужны: допуск = роль на сервере. Реализовывать OAuth на стороне
бота не требуется.

## `POST /api/v1/archive/announce`

Публикует embed в `📚┃архив`.

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/archive/announce \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{
        "number": 42,
        "title": "Дело о пропавшем свитке",
        "level": 3,
        "url": "https://veritas.example.com/dossier/42",
        "reliability": "подтверждено",
        "author": "Архивариус Вейл"
      }'
```

```json
{"ok": true, "message_id": "1234567890123456789"}
```

| Поле | Тип | Правила |
|---|---|---|
| `number` | int | 1…999999, показывается как `ДОСЬЕ-0042` |
| `title` | str | 1…200 символов |
| `level` | int | гриф 2…5 (минимальный допуск) |
| `url` | str | только `https://`, домен из `ALLOWED_SITE_DOMAINS` (или его поддомен), без логина/пароля |
| `reliability` | str | 1…60 символов («подтверждено», «вероятно», «не проверено»…) |
| `author` | str | 1…100 символов |

Лишние поля → `422`.

## `POST /api/v1/vault/announce`

Сообщение в `🗝┃хранилище` (видят уровень 4+) о новом защищённом материале. **Ключи и пароли
через бота не передаются никогда**: полей для них нет, а текст, похожий на пароль/ключ/токен,
отклоняется с `422`.

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/vault/announce \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{
        "title": "Протокол №7",
        "level": 4,
        "url": "https://veritas.example.com/vault/7",
        "author": "Архивариус Вейл",
        "note": "Новые материалы по делу о свитке"
      }'
```

| Поле | Тип | Правила |
|---|---|---|
| `title` | str | 1…200 |
| `level` | int | 4…5 |
| `url` | str | как в архиве |
| `author` | str | 1…100 |
| `note` | str, необязательно | до 500 |

## Из Discord

`/archive announce` — то же, что `POST /archive/announce`, вручную. Доступно Архивариусу и Совету.
