"""Единый источник правды Veritas: роли, допуски, каналы, пороги, настройки окружения.

Структура каналов и права ролей перенесены из эталонного ``veritas_server.py``
без изменения поведения (см. docs/DECISIONS.md, где описаны дополнения).
Здесь нет обращений к Discord — модуль можно импортировать в тестах.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

# ───────────────────── КЛЮЧИ РОЛЕЙ ─────────────────────

FOUNDER = "FOUNDER"
HEAD = "HEAD"
COUNCIL = "COUNCIL"
ARCH = "ARCH"
TRUST = "TRUST"
INIT = "INIT"
FOLL = "FOLL"
STRANGER = "STRANGER"
LIAR = "LIAR"
EXILE = "EXILE"

# Сверху вниз — так роли должны стоять в списке ролей сервера.
ROLE_ORDER: tuple[str, ...] = (FOUNDER, HEAD, COUNCIL, ARCH, TRUST, INIT, FOLL, STRANGER, LIAR, EXILE)
MANAGED_ROLES = frozenset(ROLE_ORDER)
MARK_ROLES = frozenset({LIAR, EXILE})

ROLE_TITLES = {
    FOUNDER: "👁 Основатель",
    HEAD: "👑 Глава Совета",
    COUNCIL: "⚖️ Член Совета",
    ARCH: "📚 Архивариус",
    TRUST: "🤝 Доверенный",
    INIT: "🕯 Посвящённый",
    FOLL: "📖 Последователь",
    STRANGER: "🌫 Незнакомец",
    LIAR: "🐍 Лжец",
    EXILE: "🚫 Отлучённый",
}

ROLE_IDS: dict[str, int] = {
    HEAD: 1558283519849930782,
    COUNCIL: 1558284211637461032,
    ARCH: 1558284385508134993,
    TRUST: 1558284608842502154,
    INIT: 1558284784449622107,
    STRANGER: 1558284987319455744,
    LIAR: 1558285108220399717,
    EXILE: 1558285281529040956,
}

# Ищутся по подстроке в названии (без учёта регистра).
FIND_BY_NAME: dict[str, str] = {
    FOLL: "последоват",
    FOUNDER: "основател",
}

# Последователь создаётся, если не найден.
FOLLOWER_CREATE = {"name": "📖 Последователь", "colour": 0xBDC3C7, "hoist": True}

# ───────────────────── ДОПУСКИ ─────────────────────

ROLE_CLEARANCE: dict[str, int] = {
    EXILE: 0,
    STRANGER: 1,
    FOLL: 2,
    INIT: 3,
    TRUST: 4,
    ARCH: 4,
    COUNCIL: 5,
    HEAD: 5,
    FOUNDER: 5,
}
LIAR_CEILING = 2

# ───────────────────── ПРАВА РОЛЕЙ (дословно из veritas_server.py) ─────────────────────
# «Администратор», «Управление сервером» и вебхуки — только у Основателя.
# Удалять и создавать каналы может только Глава Совета.

ROLE_PERMS: dict[str, dict[str, bool]] = {
    FOUNDER: dict(administrator=True),
    HEAD: dict(
        manage_channels=True, manage_roles=True, manage_messages=True, manage_threads=True,
        manage_nicknames=True, kick_members=True, ban_members=True, moderate_members=True,
        view_audit_log=True, mention_everyone=True, manage_events=True,
        manage_expressions=True, move_members=True, mute_members=True, deafen_members=True,
    ),
    COUNCIL: dict(
        manage_roles=True, manage_messages=True, manage_threads=True, manage_nicknames=True,
        kick_members=True, ban_members=True, moderate_members=True, view_audit_log=True,
        move_members=True, mute_members=True,
    ),
    ARCH: dict(manage_messages=True, manage_threads=True),
    TRUST: dict(moderate_members=True, manage_messages=True, manage_nicknames=True,
                mute_members=True),
    INIT: {},
    FOLL: {},
    STRANGER: {},
    LIAR: {},
    EXILE: {},
}

SAFE_EVERYONE: dict[str, bool] = dict(
    view_channel=True, send_messages=True, read_message_history=True, add_reactions=True,
    embed_links=True, attach_files=True, use_external_emojis=True, connect=True, speak=True,
    use_voice_activation=True, stream=True, use_application_commands=True,
    send_messages_in_threads=True, create_public_threads=True, change_nickname=True,
)

# Права, которые считаются «опасными» (anti-nuke снимает роли с ними,
# защита структуры следит за их появлением).
DANGEROUS_PERMS: tuple[str, ...] = (
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_webhooks",
    "ban_members", "kick_members",
)
FOUNDER_ONLY_PERMS: tuple[str, ...] = ("administrator", "manage_guild", "manage_webhooks")

# Что бот получает персональным оверрайтом в каждом своём канале — иначе в штатном
# режиме (без «Администратора») он не увидит закрытые каналы и не сможет писать в #логи.
BOT_OVERWRITE: dict[str, bool] = dict(
    view_channel=True, send_messages=True, read_message_history=True, embed_links=True,
    attach_files=True, add_reactions=True, manage_messages=True, pin_messages=True,
    send_messages_in_threads=True, manage_threads=True,
)

# ───────────────────── СТРУКТУРА КАНАЛОВ ─────────────────────

ALL = None                                           # видят все (кроме Отлучённых)
L5: tuple[str, ...] = (FOUNDER, HEAD, COUNCIL)       # Ядро
L4: tuple[str, ...] = L5 + (ARCH, TRUST)             # Тайны
L3: tuple[str, ...] = L4 + (INIT,)                   # Досье
L2: tuple[str, ...] = L3 + (FOLL,)                   # Библиотека
TOP: tuple[str, ...] = (FOUNDER, HEAD)               # Основатель и Глава Совета


@dataclass(frozen=True, eq=False)
class ChannelSpec:
    """Описание канала.

    view      — кто видит (ALL или кортеж ролей)
    write     — "all" (все, кто видит) или кортеж ролей, которые могут писать
                (для форума — кто создаёт публикации; отвечать могут все, кто видит)
    hide/show — точечно скрыть/показать канал ролям
    slow      — медленный режим, секунд
    reactions — могут ли читатели ставить реакции
    threads   — можно ли создавать ветки
    roles     — особые ограничения для отдельных ролей
    """

    key: str
    kind: str
    name: str
    topic: str | None
    view: tuple[str, ...] | None
    write: str | tuple[str, ...]
    hide: tuple[str, ...] = ()
    show: tuple[str, ...] = ()
    slow: int = 0
    reactions: bool = True
    threads: bool = True
    roles: dict[str, dict[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True, eq=False)
class CategorySpec:
    key: str
    name: str
    view: tuple[str, ...] | None
    channels: tuple[ChannelSpec, ...]


def ch(key, kind, name, topic, view, write, **kw) -> ChannelSpec:
    return ChannelSpec(key=key, kind=kind, name=name, topic=topic, view=view, write=write, **kw)


STRUCTURE: tuple[CategorySpec, ...] = (
    CategorySpec("cat_entry", "🚪 ВХОД", ALL, (
        ch("rules", "text", "📜┃правила", "Правила, о секте, ссылка на сайт.",
           ALL, TOP, reactions=False, threads=False),
        ch("verify", "text", "✅┃верификация", "Нажми кнопку и заполни анкету, чтобы войти.",
           ALL, TOP, threads=False,
           hide=(FOLL, INIT, ARCH),                     # прошедшие верификацию его не видят
           show=(TRUST, COUNCIL, HEAD, FOUNDER)),       # проверяющие видят всегда
    )),
    CategorySpec("cat_community", "💬 ОБЩИНА", L2, (
        ch("news", "news", "📢┃новости", "Объявления и вскрытые тайны.",
           L2, TOP, threads=False),
        ch("general", "text", "💬┃общий-чат", "Общение.",
           L2, "all",
           roles={LIAR: dict(attach_files=False, embed_links=False,
                             create_public_threads=False)}),
        ch("voice", "voice", "🎙┃голосовой", None,
           L2, "all", roles={LIAR: dict(stream=False)}),
        ch("requests", "text", "🎫┃заявки", "Тикеты: допуск, жалоба, вопрос Совету.",
           L2, TOP, reactions=False, threads=False),
    )),
    CategorySpec("cat_archive", "🗂 АРХИВ", L3, (
        ch("archive", "text", "📚┃архив", "Лента обновлений архива на сайте.",
           L3, (FOUNDER, HEAD, ARCH), threads=False),
        ch("intake", "text", "📥┃приём-материалов", "Сдавай информацию по шаблону из закрепа.",
           L3, "all", slow=60),
        ch("investigations", "forum", "🔎┃расследования", "Одна публикация = одно дело.",
           L3, (FOUNDER, HEAD, COUNCIL, ARCH)),
    )),
    CategorySpec("cat_secrets", "🔒 ТАЙНЫ", L4, (
        ch("applications", "text", "📋┃анкеты",
           "Анкеты новичков. Решают Доверенные и выше (кнопки бота).",
           L4, (), reactions=False, threads=False),
        ch("secrets", "text", "🕳┃тайны", "Обсуждение секретных материалов. Слив = Лжец и суд.",
           L4, "all"),
        ch("vault", "text", "🗝┃хранилище", "Ссылки на защищённые файлы. Ключи выдаёт Архивариус.",
           L4, (FOUNDER, HEAD, COUNCIL, ARCH), threads=False),
    )),
    CategorySpec("cat_council", "⚫ СОВЕТ", L5, (
        ch("council", "text", "⚫┃совет", "Обсуждения и голосования Совета.", L5, "all"),
        ch("court", "text", "⚖️┃суд-и-допуски",
           "Разбор нарушителей и журнал выдачи допусков.", L5, "all"),
        ch("logs", "text", "📊┃логи", "Логи ботов.", L5, (), reactions=False, threads=False),
    )),
    CategorySpec("cat_special", "🚫 ОСОБОЕ", L5 + (EXILE,), (
        ch("appeals", "text", "🚫┃апелляции", "Здесь Отлучённые могут просить о возвращении.",
           L5 + (EXILE,), "all", slow=600, threads=False),
    )),
    # Категория тикетов: скрыта от всех, кроме Совета и бота. Каналы создаёт бот (§6).
    CategorySpec("cat_tickets", "🎫 ТИКЕТЫ", L5, ()),
)

FORUM_TAGS: tuple[str, ...] = (
    "🟡 Открыто", "🔎 Проверяется", "✅ Подтверждено", "❌ Опровергнуто", "🔒 Закрыто",
)

# Каналы допуска 5: содержимое сообщений оттуда не логируется.
PRIVATE_CHANNEL_KEYS = frozenset({"council", "court", "logs"})


def all_channels() -> list[tuple[CategorySpec, ChannelSpec]]:
    return [(cat, c) for cat in STRUCTURE for c in cat.channels]


def channel_spec(key: str) -> ChannelSpec:
    for _, c in all_channels():
        if c.key == key:
            return c
    raise KeyError(key)


# ───────────────────── ЦВЕТА ─────────────────────

COLORS = {
    "brand": 0x2B2D42,     # фирменный (тёмный)
    "green": 0x2ECC71,     # вход / добавление
    "red": 0xE74C3C,       # удаление / бан
    "yellow": 0xF1C40F,    # изменение
    "blue": 0x3498DB,      # информация
    "gold": 0xC9A227,      # архив
    "grey": 0x95A5A6,
}

# ───────────────────── ПОРОГИ ─────────────────────


@dataclass(frozen=True)
class VerificationConfig:
    resubmit_cooldown_s: int = 10 * 60          # между подачами
    reject_cooldown_s: int = 24 * 3600          # после отказа
    max_attempts_per_day: int = 3
    min_account_age_days: float = 3.0           # моложе — ручная проверка с ⚠️
    min_reason_len: int = 30


@dataclass(frozen=True)
class TicketConfig:
    max_open_total: int = 3
    max_open_per_type: int = 1
    create_cooldown_s: int = 30
    delete_delay_s: int = 10
    inactivity_close_s: int = 7 * 24 * 3600
    inactivity_warn_before_s: int = 24 * 3600
    transcript_limit: int = 5000


@dataclass(frozen=True)
class RaidConfig:
    join_threshold: int = 8                     # N входов…
    join_window_s: int = 20                     # …за T секунд → локдаун
    auto_lift_minutes: int = 15                 # снять локдаун без новых всплесков
    lockdown_action: str = "timeout"            # "timeout" | "kick"
    lockdown_timeout_minutes: int = 60
    disable_invites: bool = True
    raise_verification: bool = True
    new_account_hours: int = 24                 # моложе — таймаут + пометка (0 = выкл)
    new_account_timeout_minutes: int = 30
    suspicion_threshold: int = 3                # скоринг подозрительности


@dataclass(frozen=True)
class SpamConfig:
    flood_messages: int = 6                     # > X сообщений…
    flood_window_s: int = 5                     # …за Y секунд
    duplicate_channels: int = 3                 # одинаковое сообщение в N каналах
    duplicate_window_s: int = 60
    max_mentions: int = 5
    offense_window_s: int = 24 * 3600
    ladder_timeouts_s: tuple[int, ...] = (0, 10 * 60, 60 * 60)  # предупреждение → 10 мин → 1 час
    link_whitelist: tuple[str, ...] = ("cdn.discordapp.com", "media.discordapp.net")
    link_exempt_channels: tuple[str, ...] = ("intake",)


@dataclass(frozen=True)
class NukeConfig:
    window_s: int = 60
    threshold: int = 3
    timeout_minutes: int = 60 * 24


@dataclass(frozen=True)
class RankConfig:
    trusted_timeout_max_s: int = 24 * 3600
    discord_timeout_max_s: int = 28 * 24 * 3600
    warn_hint_threshold: int = 3
    active_days_hint: int = 7


VERIFICATION = VerificationConfig()
TICKETS = TicketConfig()
RAID = RaidConfig()
SPAM = SpamConfig()
NUKE = NukeConfig()
RANKS = RankConfig()

# Категории логов и значения по умолчанию.
LOG_CATEGORIES: dict[str, bool] = {
    "members": True,
    "roles": True,
    "messages": True,
    "server": True,
    "voice": False,
    "bot": True,
}


def override_dataclass(obj, values: dict[str, str]):
    """Применить строковые значения (из БД) к frozen-dataclass с приведением типов."""
    kw = {}
    names = {f.name: f for f in fields(obj)}
    for k, v in values.items():
        if k not in names:
            continue
        cur = getattr(obj, k)
        if isinstance(cur, bool):
            kw[k] = str(v).lower() in ("1", "true", "yes", "on", "да")
        elif isinstance(cur, int):
            kw[k] = int(v)
        elif isinstance(cur, float):
            kw[k] = float(v)
        elif isinstance(cur, str):
            kw[k] = str(v)
    return replace(obj, **kw)


# ───────────────────── ОКРУЖЕНИЕ ─────────────────────


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Минимальный загрузчик .env (без зависимостей). Не перезаписывает уже заданные переменные."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass(frozen=True)
class Settings:
    discord_token: str = ""
    guild_id: int = 0
    api_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    allowed_site_domains: tuple[str, ...] = ()
    verify_mode: str = "manual"
    log_level: str = "INFO"
    db_path: str = "data/veritas.db"
    restore_on_rejoin: bool = False
    head_can_appoint_council: bool = False
    site_url: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        mode = (e.get("VERIFY_MODE") or "manual").strip().lower()
        if mode not in ("manual", "auto"):
            mode = "manual"
        domains = tuple(
            d.strip().lower() for d in (e.get("ALLOWED_SITE_DOMAINS") or "").split(",") if d.strip()
        )
        gid = (e.get("GUILD_ID") or "0").strip()
        return cls(
            discord_token=(e.get("DISCORD_TOKEN") or "").strip(),
            guild_id=int(gid) if gid.isdigit() else 0,
            api_token=(e.get("API_TOKEN") or "").strip(),
            api_host=(e.get("API_HOST") or "127.0.0.1").strip(),
            api_port=int((e.get("API_PORT") or "8080").strip()),
            allowed_site_domains=domains,
            verify_mode=mode,
            log_level=(e.get("LOG_LEVEL") or "INFO").strip().upper(),
            db_path=(e.get("DB_PATH") or "data/veritas.db").strip(),
            restore_on_rejoin=_bool(e.get("RESTORE_ON_REJOIN")),
            head_can_appoint_council=_bool(e.get("HEAD_CAN_APPOINT_COUNCIL")),
            site_url=(e.get("SITE_URL") or "").strip(),
        )

    def __repr__(self) -> str:  # никогда не печатаем секреты
        return (f"Settings(guild_id={self.guild_id}, verify_mode={self.verify_mode!r}, "
                f"api={self.api_host}:{self.api_port}, db={self.db_path!r}, "
                f"token={'set' if self.discord_token else 'unset'}, "
                f"api_token={'set' if self.api_token else 'unset'})")

    __str__ = __repr__
