"""Резервная копия базы в самом Discord — для хостингов без постоянного диска (Render Free).

На бесплатном Render файлы контейнера стираются при каждом деплое и перезапуске. Поэтому бот:

* при старте, если локальной базы нет, скачивает последнюю копию из скрытого канала
  ``🗄┃бэкап-бота`` (видит только бот; Основатель — как администратор);
* раз в ``DB_BACKUP_MINUTES`` минут и при остановке (SIGTERM) загружает свежую копию, **редактируя
  одно и то же сообщение** — ничего не удаляется и не копится.

Если скачать копию не удалось из-за ошибки Discord, сохранение в этом запуске выключается,
чтобы пустая база не затёрла хорошую копию.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import VeritasBot

log = logging.getLogger("veritas.cloud_backup")

CHANNEL_NAME = "🗄┃бэкап-бота"
FILENAME = "veritas-backup.db"
MAX_BYTES = 9 * 1024 * 1024  # лимит вложения для ботов — 10 МБ


def snapshot_bytes(db_path: str | Path) -> bytes:
    """Согласованный снимок SQLite (безопасно при открытой базе в режиме WAL)."""
    src = Path(db_path)
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=src.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(tmp)) as d:
            s.backup(d)
        return Path(tmp).read_bytes()
    finally:
        Path(tmp).unlink(missing_ok=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sqlite(data: bytes) -> bool:
    return data.startswith(b"SQLite format 3\x00")


class DiscordBackup:
    def __init__(self, bot: VeritasBot):
        self.bot = bot
        self.channel_id: int | None = None
        self.message_id: int | None = None
        self.last_digest: str | None = None
        self.enabled = True
        self.last_saved_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return Path(self.bot.settings.db_path)

    # ── поиск канала и сообщения

    async def _guild(self) -> discord.Guild:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        return guild or await self.bot.fetch_guild(self.bot.settings.guild_id)

    async def _find_channel(self, *, create: bool) -> discord.TextChannel | None:
        if self.channel_id:
            ch = self.bot.get_channel(self.channel_id)
            if ch is not None:
                return ch
        guild = await self._guild()
        channels = guild.channels if self.bot.get_guild(guild.id) else await guild.fetch_channels()
        ch = next((c for c in channels
                   if getattr(c, "type", None) == discord.ChannelType.text and c.name == CHANNEL_NAME), None)
        if ch is None and create:
            ch = await guild.create_text_channel(
                CHANNEL_NAME, topic="Резервная копия базы бота. Не удаляйте это сообщение.",
                overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False),
                            discord.Object(id=self.bot.user.id): discord.PermissionOverwrite(
                                view_channel=True, send_messages=True, attach_files=True,
                                read_message_history=True)},
                reason="Veritas: хранение резервной копии базы")
            log.info("Создан канал %s для резервной копии базы", CHANNEL_NAME)
        if ch is not None:
            self.channel_id = ch.id
        return ch

    async def _find_message(self, channel: discord.TextChannel) -> discord.Message | None:
        async for m in channel.history(limit=50):
            if m.author.id == self.bot.user.id and any(a.filename == FILENAME for a in m.attachments):
                return m
        return None

    # ── восстановление

    async def restore_if_missing(self) -> str:
        path = self.db_path
        if path.exists() and path.stat().st_size > 0:
            return "локальная база на месте — восстановление не нужно"
        try:
            channel = await self._find_channel(create=False)
            msg = await self._find_message(channel) if channel is not None else None
            if msg is None:
                return "копии в Discord ещё нет — начинаю с пустой базы"
            att = next(a for a in msg.attachments if a.filename == FILENAME)
            data = await att.read()
        except discord.HTTPException as exc:
            self.enabled = False
            log.error("Не удалось скачать копию базы (%s). Сохранение в этом запуске ВЫКЛЮЧЕНО, "
                      "чтобы не затереть копию пустой базой. Перезапусти сервис.", exc)
            return "ошибка восстановления — сохранение выключено"
        if not is_sqlite(data):
            self.enabled = False
            log.error("Вложение %s не похоже на базу SQLite — сохранение выключено", FILENAME)
            return "копия повреждена — сохранение выключено"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.message_id = msg.id
        self.last_digest = digest(data)
        return f"база восстановлена из Discord ({len(data) // 1024} КБ, от {msg.edited_at or msg.created_at:%Y-%m-%d %H:%M} UTC)"

    # ── сохранение

    async def save(self) -> bool:
        """Загрузить копию, если база изменилась. True — если загрузили."""
        if not self.enabled or not self.db_path.exists():
            return False
        async with self._lock:
            data = await asyncio.to_thread(snapshot_bytes, self.db_path)
            d = digest(data)
            if d == self.last_digest:
                return False
            if len(data) > MAX_BYTES:
                log.error("База %d КБ больше лимита вложения Discord — копия не сохранена", len(data) // 1024)
                return False
            channel = await self._find_channel(create=True)
            content = (f"🗄 Резервная копия базы Veritas · <t:{int(time.time())}:f> · {len(data) // 1024} КБ\n"
                       "Не удаляйте это сообщение: из него бот восстанавливает базу после перезапуска.")
            file = discord.File(io.BytesIO(data), filename=FILENAME)
            msg = None
            if self.message_id is None:
                found = await self._find_message(channel)
                self.message_id = found.id if found else None
            if self.message_id is not None:
                try:
                    msg = await channel.get_partial_message(self.message_id).edit(content=content, attachments=[file])
                except discord.NotFound:
                    self.message_id = None
                    file = discord.File(io.BytesIO(data), filename=FILENAME)
            if msg is None:
                msg = await channel.send(content, file=file)
                self.message_id = msg.id
            self.last_digest = d
            self.last_saved_at = time.time()
            log.info("Копия базы сохранена в Discord (%d КБ)", len(data) // 1024)
            return True

    async def run_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval = self.bot.settings.backup_minutes * 60
        while not self.bot.is_closed():
            await asyncio.sleep(interval)
            try:
                await self.save()
            except Exception:  # noqa: BLE001 — следующий заход попробует снова
                log.exception("Не удалось сохранить копию базы в Discord")
