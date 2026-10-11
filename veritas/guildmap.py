"""Связка «ключи из config ↔ объекты Discord» для одного сервера."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from . import config
from .clearance import clearance_level

if TYPE_CHECKING:
    import discord


def resolve_role_ids(
    roles: Iterable[tuple[int, str]],
    role_ids: dict[str, int] | None = None,
    find_by_name: dict[str, str] | None = None,
    cached: dict[str, int] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Найти ID ролей: сначала по ID из config, затем по сохранённому ID, затем по названию.

    ``roles`` — пары (id, название) в порядке сверху вниз.
    Возвращает (ключ → id, ключи из ROLE_IDS, которых нет на сервере).
    """
    role_ids = config.ROLE_IDS if role_ids is None else role_ids
    find_by_name = config.FIND_BY_NAME if find_by_name is None else find_by_name
    roles = list(roles)
    present = {rid for rid, _ in roles}
    result: dict[str, int] = {}
    missing: list[str] = []
    for key, rid in role_ids.items():
        if rid in present:
            result[key] = rid
        else:
            missing.append(key)
    for key, needle in find_by_name.items():
        if key in result:
            continue
        if cached and cached.get(key) in present:
            result[key] = cached[key]
            continue
        taken = set(result.values())
        for rid, name in roles:
            if needle in name.lower() and rid not in taken:
                result[key] = rid
                break
    return result, missing


class GuildMap:
    """Резолвит ключи ролей и каналов в объекты конкретного сервера."""

    def __init__(self, guild: discord.Guild, role_ids: dict[str, int], channel_ids: dict[str, int]):
        self.guild = guild
        self.role_ids = dict(role_ids)
        self.channel_ids = dict(channel_ids)
        self._key_by_role = {rid: key for key, rid in self.role_ids.items()}

    def role(self, key: str) -> discord.Role | None:
        rid = self.role_ids.get(key)
        return self.guild.get_role(rid) if rid else None

    def roles(self, keys: Iterable[str]) -> list[discord.Role]:
        return [r for r in (self.role(k) for k in keys) if r is not None]

    def key_of(self, role_id: int) -> str | None:
        return self._key_by_role.get(role_id)

    def member_keys(self, member: discord.abc.User) -> set[str]:
        keys = {self._key_by_role[r.id] for r in getattr(member, "roles", ()) if r.id in self._key_by_role}
        if member.id == self.guild.owner_id:
            keys.add(config.FOUNDER)  # владелец сервера = Основатель
        return keys

    def level(self, member: discord.abc.User) -> int:
        return clearance_level(self.member_keys(member))

    def channel(self, key: str):
        cid = self.channel_ids.get(key)
        return self.guild.get_channel(cid) if cid else None

    def channel_key(self, channel_id: int) -> str | None:
        for key, cid in self.channel_ids.items():
            if cid == channel_id:
                return key
        return None
