"""Кэш записей журнала аудита: «кто это сделал» без лишних запросов к API.

Основной источник — событие ``on_audit_log_entry_create`` (приходит само).
Если записи нет, бот один раз аккуратно запрашивает журнал: не чаще раза в
``min_interval`` секунд, с таймаутом, ошибки не роняют обработчик.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

import discord

log = logging.getLogger("veritas.audit")


@dataclass(frozen=True)
class AuditRecord:
    action: str
    target_id: int | None
    user_id: int | None
    user: str | None
    reason: str | None
    ts: float


class AuditCache:
    def __init__(self, *, ttl: float = 60.0, max_entries: int = 500, min_interval: float = 2.0,
                 clock=time.monotonic):
        self.ttl = ttl
        self.records: deque[AuditRecord] = deque(maxlen=max_entries)
        self.min_interval = min_interval
        self.clock = clock
        self._last_fetch = -1e9
        self._lock = asyncio.Lock()

    def add(self, action: str, target_id: int | None, user_id: int | None, user: str | None,
            reason: str | None) -> AuditRecord:
        rec = AuditRecord(action, target_id, user_id, user, reason, self.clock())
        self.records.append(rec)
        return rec

    def add_entry(self, entry: discord.AuditLogEntry) -> AuditRecord:
        target = getattr(entry, "target", None)
        user = entry.user
        return self.add(entry.action.name, getattr(target, "id", None), getattr(user, "id", entry.user_id),
                        user.mention if user is not None else (f"<@{entry.user_id}>" if entry.user_id else None),
                        entry.reason)

    def find(self, actions: str | tuple[str, ...], target_id: int | None, within: float | None = None
             ) -> AuditRecord | None:
        actions = (actions,) if isinstance(actions, str) else actions
        horizon = self.clock() - (within if within is not None else self.ttl)
        for rec in reversed(self.records):
            if rec.ts < horizon:
                break
            if rec.action in actions and (target_id is None or rec.target_id == target_id):
                return rec
        return None

    async def lookup(self, guild: discord.Guild, actions: str | tuple[str, ...], target_id: int | None, *,
                     wait: float = 1.5, within: float = 15.0) -> AuditRecord | None:
        rec = self.find(actions, target_id, within)
        if rec is not None:
            return rec
        await asyncio.sleep(wait)  # событие аудита обычно приходит чуть позже
        rec = self.find(actions, target_id, within)
        if rec is not None:
            return rec
        return await self._fetch(guild, actions, target_id, within)

    async def _fetch(self, guild, actions, target_id, within) -> AuditRecord | None:
        actions = (actions,) if isinstance(actions, str) else actions
        async with self._lock:
            if self.clock() - self._last_fetch < self.min_interval:
                return self.find(actions, target_id, within)
            self._last_fetch = self.clock()
            try:
                async def collect():
                    async for entry in guild.audit_logs(limit=10, action=getattr(discord.AuditLogAction, actions[0])):
                        if entry.created_at.timestamp() < time.time() - within:
                            break
                        self.add_entry(entry)
                await asyncio.wait_for(collect(), timeout=5)
            except (discord.Forbidden, discord.HTTPException, TimeoutError):
                log.debug("Журнал аудита недоступен", exc_info=True)
        return self.find(actions, target_id, within)
