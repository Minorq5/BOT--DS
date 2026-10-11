"""Чистая логика прав: расчёт overwrites по структуре и симуляция итоговых прав.

Ни одного обращения к Discord: цели overwrites — строки
``"@everyone"``, ``"@bot"`` или ключи ролей из ``config``.
Перенос ``category_overwrites``/``channel_overwrites`` из ``veritas_server.py``
с тем же поведением; добавлен только оверрайт бота (см. DECISIONS D-101).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from . import config
from .config import ALL, EXILE, CategorySpec, ChannelSpec

EVERYONE = "@everyone"
BOT = "@bot"

Overwrite = dict[str, bool]          # право → разрешено/запрещено (нет ключа — «наследовать»)
Overwrites = dict[str, Overwrite]    # цель → оверрайт


def _o(ow: Overwrites, target: str) -> Overwrite:
    return ow.setdefault(target, {})


def category_overwrites(cat: CategorySpec, *, include_bot: bool = True) -> Overwrites:
    ow: Overwrites = {}
    if cat.view is ALL:
        ow[EVERYONE] = {"view_channel": True}
        ow[EXILE] = {"view_channel": False}
    else:
        ow[EVERYONE] = {"view_channel": False}
        for n in cat.view:
            ow[n] = {"view_channel": True}
    if include_bot:
        ow[BOT] = dict(config.BOT_OVERWRITE)
        if cat.key == "cat_tickets":
            # бот создаёт и закрывает тикеты внутри этой категории
            ow[BOT].update(manage_channels=True, manage_permissions=True)
    return ow


def channel_overwrites(c: ChannelSpec, *, include_bot: bool = True) -> Overwrites:
    ow: Overwrites = {}
    every = _o(ow, EVERYONE)

    # кто видит
    if c.view is ALL:
        every["view_channel"] = True
        _o(ow, EXILE)["view_channel"] = False
    else:
        every["view_channel"] = False
        for n in c.view:
            _o(ow, n)["view_channel"] = True
    for n in c.hide:
        _o(ow, n)["view_channel"] = False
    for n in c.show:
        _o(ow, n)["view_channel"] = True

    # кто пишет
    if c.write != "all":
        every["send_messages"] = False
        for n in c.write:
            _o(ow, n).update(view_channel=True, send_messages=True)

    # ветки
    if not c.threads and c.kind != "forum":
        every.update(create_public_threads=False, create_private_threads=False)

    # реакции
    if not c.reactions:
        every["add_reactions"] = False
        if c.write != "all":
            for n in c.write:
                _o(ow, n)["add_reactions"] = True

    # особые ограничения ролей
    for n, kw in c.roles.items():
        _o(ow, n).update(kw)

    if include_bot:
        ow[BOT] = dict(config.BOT_OVERWRITE)
    return ow


def compute_overwrites(structure: Iterable[CategorySpec] = config.STRUCTURE, *,
                       include_bot: bool = True) -> dict[str, Overwrites]:
    """Ключ категории/канала → overwrites. Единственный источник правды по правам каналов."""
    result: dict[str, Overwrites] = {}
    for cat in structure:
        result[cat.key] = category_overwrites(cat, include_bot=include_bot)
        for c in cat.channels:
            result[c.key] = channel_overwrites(c, include_bot=include_bot)
    return result


def ticket_overwrites(handlers: Iterable[str], author: str = "@author") -> Overwrites:
    """Права приватного канала тикета: видят автор, обработчики и бот."""
    member_perms = dict(view_channel=True, send_messages=True, read_message_history=True,
                        attach_files=True, embed_links=True, add_reactions=True)
    ow: Overwrites = {EVERYONE: {"view_channel": False}, author: dict(member_perms)}
    for h in handlers:
        ow[h] = dict(member_perms)
    ow[BOT] = dict(config.BOT_OVERWRITE)
    ow[BOT].update(manage_channels=True, manage_permissions=True)
    return ow


# ───────────────────── СИМУЛЯЦИЯ ПРАВ ─────────────────────


def base_permissions(role_keys: Iterable[str],
                     everyone: Mapping[str, bool] = config.SAFE_EVERYONE,
                     role_perms: Mapping[str, Mapping[str, bool]] = config.ROLE_PERMS) -> set[str]:
    """Права на уровне сервера: @everyone + все роли человека."""
    perms = {p for p, v in everyone.items() if v}
    for k in role_keys:
        perms |= {p for p, v in role_perms.get(k, {}).items() if v}
    return perms


def effective_permissions(role_keys: Iterable[str], overwrites: Overwrites, *,
                          everyone: Mapping[str, bool] = config.SAFE_EVERYONE,
                          role_perms: Mapping[str, Mapping[str, bool]] = config.ROLE_PERMS,
                          member_target: str | None = None) -> set[str] | None:
    """Итоговые права в канале по алгоритму Discord.

    Возвращает множество разрешённых прав или ``None``, если у человека «Администратор»
    (он может всё).
    """
    keys = set(role_keys)
    perms = base_permissions(keys, everyone, role_perms)
    if "administrator" in perms:
        return None
    # 1. @everyone
    ev = overwrites.get(EVERYONE, {})
    perms -= {p for p, v in ev.items() if v is False}
    perms |= {p for p, v in ev.items() if v is True}
    # 2. роли: сначала все запреты, потом все разрешения (разрешение побеждает)
    deny, allow = set(), set()
    for k in keys:
        ow = overwrites.get(k, {})
        deny |= {p for p, v in ow.items() if v is False}
        allow |= {p for p, v in ow.items() if v is True}
    perms = (perms - deny) | allow
    # 3. персональный оверрайт
    if member_target is not None:
        ow = overwrites.get(member_target, {})
        perms -= {p for p, v in ow.items() if v is False}
        perms |= {p for p, v in ow.items() if v is True}
    # без просмотра нет ничего
    if "view_channel" not in perms:
        return set()
    return perms


HIDDEN, READ, WRITE = "—", "R", "W"


def access(role_keys: Iterable[str], c: ChannelSpec, overwrites: Overwrites | None = None) -> str:
    """«не видит» / «читает» / «пишет» для канала.

    Текст/объявления: пишет = send_messages. Форум: пишет = создаёт публикации.
    Голосовой: пишет = может подключиться и говорить.
    """
    ow = overwrites if overwrites is not None else channel_overwrites(c)
    perms = effective_permissions(role_keys, ow)
    if perms is None:
        return WRITE
    if "view_channel" not in perms:
        return HIDDEN
    if c.kind == "voice":
        return WRITE if {"connect", "speak"} <= perms else READ
    return WRITE if "send_messages" in perms else READ


def can(role_keys: Iterable[str], c: ChannelSpec, perm: str) -> bool:
    perms = effective_permissions(role_keys, channel_overwrites(c))
    return perms is None or perm in perms
