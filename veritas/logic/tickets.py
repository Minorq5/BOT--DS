"""Тикеты: типы, лимиты, имена каналов, неактивность, транскрипт. Без Discord."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..config import COUNCIL, FOUNDER, HEAD, ROLE_CLEARANCE, TicketConfig

OPEN, CLOSED = "open", "closed"


@dataclass(frozen=True)
class TicketType:
    key: str
    label: str
    emoji: str
    handler_level: int
    hint: str
    handlers: tuple[str, ...] = ()   # явный список обработчиков (иначе — все с нужным допуском)
    prefix: str = "тикет"            # начало названия канала
    on_panel: bool = True            # кнопка на панели #заявки
    ping_handlers: bool = False      # тегнуть обработчиков при открытии


TICKET_TYPES: dict[str, TicketType] = {
    "access": TicketType("access", "Заявка на допуск", "🔑", 4,
                         "Опиши свой вклад и какой допуск просишь. Повышение выдаётся командой "
                         "`/rank promote` или `/rank set` — обработчик сделает это после решения."),
    "complaint": TicketType("complaint", "Жалоба", "⚠️", 5,
                            "Опиши, что произошло, кто участвовал, приложи скриншоты. "
                            "Совет разберёт жалобу и ответит здесь."),
    "question": TicketType("question", "Вопрос Совету", "❓", 5,
                           "Задай вопрос. Ответит Член Совета."),
    "materials": TicketType("materials", "Отправка материала", "📥", 5,
                            "Проверь материал, при необходимости задай уточняющие вопросы, назначь награду "
                            "и закрой тикет. Подтверждённое — в архив через `/archive announce`.",
                            handlers=(HEAD, FOUNDER), prefix="материал", on_panel=False, ping_handlers=True),
}


def handler_roles(ticket_type: str) -> list[str]:
    """Роли-обработчики: явный список типа или все с допуском не ниже требуемого."""
    t = TICKET_TYPES[ticket_type]
    if t.handlers:
        return list(t.handlers)
    level = t.handler_level
    order = ("TRUST", "ARCH", COUNCIL, HEAD, FOUNDER)
    return [k for k in order if ROLE_CLEARANCE.get(k, 0) >= level]


def check_limits(open_types: Iterable[str], ticket_type: str, last_created: float | None,
                 now: float, cfg: TicketConfig) -> tuple[bool, str]:
    open_types = list(open_types)
    if last_created is not None and now - last_created < cfg.create_cooldown_s:
        wait = int(cfg.create_cooldown_s - (now - last_created)) + 1
        return False, f"Подожди {wait} с перед новым обращением."
    if open_types.count(ticket_type) >= cfg.max_open_per_type:
        return False, f"У тебя уже открыт тикет «{TICKET_TYPES[ticket_type].label}». Продолжи в нём."
    if len(open_types) >= cfg.max_open_total:
        return False, f"Открыто уже {len(open_types)} тикета — это максимум. Закрой один из них."
    return True, ""


_SLUG_BAD = re.compile(r"[^a-z0-9а-яё]+")


def slugify(name: str, limit: int = 20) -> str:
    s = _SLUG_BAD.sub("-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:limit].strip("-")
    return s or "user"


def channel_name(number: int, nickname: str, prefix: str = "тикет") -> str:
    return f"{prefix}-{number:04d}-{slugify(nickname)}"


def is_handler(role_keys: Iterable[str], ticket_type: str) -> bool:
    """Обработчик = есть роль из списка обработчиков и допуск не ниже требуемого (Лжец отсекается)."""
    from ..clearance import clearance_level

    keys = set(role_keys)
    t = TICKET_TYPES[ticket_type]
    return bool(keys & set(handler_roles(ticket_type))) and clearance_level(keys) >= t.handler_level


def inactivity_action(last_activity: float, warned: bool, now: float, cfg: TicketConfig) -> str | None:
    idle = now - last_activity
    if idle >= cfg.inactivity_close_s:
        return "close"
    if not warned and idle >= cfg.inactivity_close_s - cfg.inactivity_warn_before_s:
        return "warn"
    return None


# ───────────────────── ТРАНСКРИПТ ─────────────────────


@dataclass(frozen=True)
class TranscriptMessage:
    ts: datetime
    author: str
    author_id: int
    content: str
    attachments: list[str] = field(default_factory=list)
    embeds: int = 0


def build_transcript(header: dict[str, str], messages: Iterable[TranscriptMessage]) -> str:
    lines = ["VERITAS · транскрипт тикета", "=" * 40]
    lines += [f"{k}: {v}" for k, v in header.items()]
    lines.append("=" * 40)
    count = 0
    for m in messages:
        count += 1
        stamp = m.ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"[{stamp}] {m.author} ({m.author_id}):")
        if m.content:
            lines.extend("    " + line for line in m.content.splitlines())
        for url in m.attachments:
            lines.append(f"    📎 {url}")
        if m.embeds:
            lines.append(f"    [embed × {m.embeds}]")
    lines.append("=" * 40)
    lines.append(f"Сообщений: {count}")
    return "\n".join(lines) + "\n"

