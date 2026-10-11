"""Логи: фильтр приватности, форматирование embed-ов, приглашения. Без обращений к сети.

Функции принимают простые значения (строки, id, даты), поэтому их легко тестировать.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import discord

from ..embeds import clip, make_embed

# Цвета по смыслу (§7): зелёный — вход/добавление, красный — удаление/бан,
# жёлтый — изменение, синий — инфо.
GREEN, RED, YELLOW, BLUE = "green", "red", "yellow", "blue"

PERM_NAMES = {
    "administrator": "Администратор",
    "manage_guild": "Управление сервером",
    "manage_roles": "Управление ролями",
    "manage_channels": "Управление каналами",
    "manage_webhooks": "Управление вебхуками",
    "manage_messages": "Управление сообщениями",
    "manage_threads": "Управление ветками",
    "manage_nicknames": "Управление никами",
    "manage_expressions": "Управление эмодзи",
    "manage_events": "Управление событиями",
    "kick_members": "Кик",
    "ban_members": "Бан",
    "moderate_members": "Тайм-ауты",
    "view_audit_log": "Журнал аудита",
    "mention_everyone": "Упоминание @everyone",
    "view_channel": "Просмотр каналов",
    "send_messages": "Отправка сообщений",
    "attach_files": "Файлы",
    "embed_links": "Ссылки",
}


def perm_name(p: str) -> str:
    return PERM_NAMES.get(p, p)


# ───────────────────── ПРИВАТНОСТЬ ─────────────────────


def content_allowed(channel_id: int, *, parent_id: int | None = None, category_id: int | None = None,
                    private_ids: Iterable[int] = (), ticket_category_id: int | None = None,
                    ticket_channel_ids: Iterable[int] = ()) -> bool:
    """Можно ли логировать содержимое сообщения из этого канала.

    Нельзя: каналы допуска 5 (совет, суд, логи), тикеты (по категории или по списку из БД),
    ветки внутри них.
    """
    private = set(private_ids) | set(ticket_channel_ids)
    if channel_id in private or (parent_id is not None and parent_id in private):
        return False
    return not (ticket_category_id is not None and category_id == ticket_category_id)


# ───────────────────── ПРИГЛАШЕНИЯ ─────────────────────


def find_used_invite(before: dict[str, int], after: dict[str, int]) -> str | None:
    """Код приглашения, у которого выросло число использований ровно у одного."""
    grown = [code for code, uses in after.items() if uses > before.get(code, 0)]
    if len(grown) == 1:
        return grown[0]
    vanished = [code for code in before if code not in after]
    if not grown and len(vanished) == 1:
        return vanished[0]  # одноразовое приглашение исчезло после использования
    return None


def perm_diff(before: int, after: int) -> tuple[list[str], list[str]]:
    b, a = discord.Permissions(before), discord.Permissions(after)
    added = [n for n, v in a if v and not getattr(b, n)]
    removed = [n for n, v in b if v and not getattr(a, n)]
    return added, removed


# ───────────────────── EMBED-Ы ─────────────────────


def _ts(dt: datetime | None, style: str = "f") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "неизвестно"


def _actor(actor: str | None) -> tuple[str, str, bool]:
    return ("Кто", actor or "не определён", True)


def member_join(mention: str, user_id: int, created_at: datetime | None, now: datetime,
                inviter: str | None = None, flags: Iterable[str] = ()) -> discord.Embed:
    age_days = (now - created_at).total_seconds() / 86400 if created_at else None
    fields = [("Участник", f"{mention} (`{user_id}`)", False),
              ("Аккаунт создан", f"{_ts(created_at)} ({_ts(created_at, 'R')})", False)]
    if inviter:
        fields.append(("Приглашение", inviter, False))
    flags = list(flags)
    if age_days is not None and age_days < 7:
        flags.insert(0, f"🆕 аккаунту {age_days:.1f} дн.")
    if flags:
        fields.append(("Пометки", "\n".join(flags), False))
    return make_embed("📥 Вход на сервер", None, color=GREEN, fields=fields, footer=f"ID: {user_id}")


def member_leave(mention: str, user_id: int, joined_at: datetime | None, roles: Iterable[str]) -> discord.Embed:
    roles = list(roles)
    return make_embed("📤 Выход с сервера", None, color=RED, footer=f"ID: {user_id}", fields=[
        ("Участник", f"{mention} (`{user_id}`)", False),
        ("Был с нами с", _ts(joined_at), True),
        ("Роли", ", ".join(roles) if roles else "—", False)])


def moderation(title: str, mention: str, user_id: int, actor: str | None, reason: str | None,
               color: str = RED, extra: Iterable[tuple[str, str, bool]] = ()) -> discord.Embed:
    fields = [("Участник", f"{mention} (`{user_id}`)", True), _actor(actor)]
    fields += list(extra)
    fields.append(("Причина", reason or "не указана", False))
    return make_embed(title, None, color=color, fields=fields, footer=f"ID: {user_id}")


def timeout_change(mention: str, user_id: int, until: datetime | None, actor: str | None,
                   reason: str | None) -> discord.Embed:
    if until is None:
        return moderation("🔊 Тайм-аут снят", mention, user_id, actor, reason, color=GREEN)
    return moderation("🔇 Тайм-аут", mention, user_id, actor, reason, color=YELLOW,
                      extra=[("До", f"{_ts(until)} ({_ts(until, 'R')})", False)])


def nick_change(mention: str, user_id: int, before: str | None, after: str | None) -> discord.Embed:
    return make_embed("✏️ Смена ника", None, color=YELLOW, footer=f"ID: {user_id}", fields=[
        ("Участник", mention, False), ("Было", before or "—", True), ("Стало", after or "—", True)])


def roles_change(mention: str, user_id: int, added: Iterable[str], removed: Iterable[str],
                 actor: str | None) -> discord.Embed:
    added, removed = list(added), list(removed)
    color = GREEN if added and not removed else RED if removed and not added else YELLOW
    fields = [("Участник", f"{mention} (`{user_id}`)", True), _actor(actor)]
    if added:
        fields.append(("➕ Выданы", ", ".join(added), False))
    if removed:
        fields.append(("➖ Сняты", ", ".join(removed), False))
    return make_embed("🎭 Роли участника", None, color=color, fields=fields, footer=f"ID: {user_id}")


PRIVATE_NOTE = "🔒 Содержимое не логируется (приватный канал)"
NO_INTENT_NOTE = "Содержимое недоступно (выключен Message Content Intent)"
NOT_CACHED_NOTE = "Сообщение не в кэше (отправлено до запуска бота или давно) — содержимое неизвестно"


def message_delete(channel: str, author: str | None, author_id: int | None, content: str | None,
                   attachments: Iterable[str], *, cached: bool, private: bool,
                   content_available: bool = True) -> discord.Embed:
    fields = [("Канал", channel, True), ("Автор", author or "неизвестен", True)]
    if not cached:
        body = NOT_CACHED_NOTE
    elif private:
        body = PRIVATE_NOTE
    elif not content_available:
        body = NO_INTENT_NOTE
    else:
        body = clip(content or "*(без текста)*", 1000)
        attachments = list(attachments)
        if attachments:
            fields.append(("Вложения", clip("\n".join(attachments)), False))
    return make_embed("🗑 Сообщение удалено", body, color=RED, fields=fields,
                      footer=f"ID автора: {author_id}" if author_id else None)


def message_edit(channel: str, author: str, author_id: int, before: str | None, after: str | None,
                 jump_url: str | None, *, private: bool, content_available: bool = True) -> discord.Embed:
    fields = [("Канал", channel, True), ("Автор", author, True)]
    if private:
        desc = PRIVATE_NOTE
    elif not content_available:
        desc = NO_INTENT_NOTE
    else:
        desc = None
        fields.append(("Было", clip(before) if before is not None else NOT_CACHED_NOTE, False))
        fields.append(("Стало", clip(after or "*(без текста)*"), False))
    if jump_url:
        fields.append(("Сообщение", f"[перейти]({jump_url})", False))
    return make_embed("✏️ Сообщение изменено", desc, color=YELLOW, fields=fields, footer=f"ID автора: {author_id}")


def bulk_delete(channel: str, count: int) -> discord.Embed:
    return make_embed("🧹 Массовое удаление", f"В {channel} удалено сообщений: **{count}**.", color=RED)


def server_change(title: str, color: str, lines: Iterable[str], actor: str | None = None) -> discord.Embed:
    lines = [x for x in lines if x]
    fields = [_actor(actor)] if actor is not None else []
    return make_embed(title, "\n".join(lines) or None, color=color, fields=fields)


def role_update(name: str, added: Iterable[str], removed: Iterable[str], other: Iterable[str],
                actor: str | None) -> discord.Embed:
    lines = list(other)
    added, removed = list(added), list(removed)
    if added:
        lines.append("➕ Права: " + ", ".join(perm_name(p) for p in added))
    if removed:
        lines.append("➖ Права: " + ", ".join(perm_name(p) for p in removed))
    danger = {"administrator", "manage_guild", "manage_roles", "manage_webhooks", "manage_channels"}
    title = "⚠️ Роль получила опасные права" if danger & set(added) else "🎭 Роль изменена"
    return server_change(f"{title}: {name}", RED if danger & set(added) else YELLOW, lines, actor)


def info(title: str, description: str, color: str = BLUE) -> discord.Embed:
    return make_embed(title, description, color=color)
