"""Архив: объявления в #архив и #хранилище, мост с сайтом (HTTP API)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..api.app import BridgeUnavailable, create_app
from ..api.models import (
    ArchiveAnnouncement,
    ClearanceResponse,
    VaultAnnouncement,
    dossier_code,
    secret_error,
    url_error,
)
from ..api.server import make_server
from ..clearance import clearance_level
from ..content import texts
from ..embeds import make_embed
from ..ui import Denied, member_keys

if TYPE_CHECKING:
    from ..bot import VeritasBot

log = logging.getLogger("veritas.archive")

ARCHIVISTS = {config.ARCH, config.COUNCIL, config.HEAD, config.FOUNDER}


def archive_embed(item: ArchiveAnnouncement) -> discord.Embed:
    e = make_embed(f"📁 {dossier_code(item.number)} · {item.title}", None, color="gold", fields=[
        ("Гриф", f"{texts.GRIF.get(item.level, item.level)} · допуск {item.level}+", True),
        ("Достоверность", item.reliability, True),
        ("Автор", item.author, True),
        ("На сайте", item.url, False)])
    e.url = item.url
    return e


def vault_embed(item: VaultAnnouncement) -> discord.Embed:
    fields = [("Гриф", f"{texts.GRIF.get(item.level, item.level)} · допуск {item.level}+", True),
              ("Автор", item.author, True), ("Ссылка", item.url, False)]
    if item.note:
        fields.append(("Описание", item.note, False))
    fields.append(("Доступ", texts.VAULT_KEYS_NOTE, False))
    e = make_embed(f"🗝 Новый защищённый материал · {item.title}", None, color="brand", fields=fields)
    e.url = item.url
    return e


class BotBridge:
    """Связка API ↔ бот. Использует ту же функцию допуска, что и бот."""

    def __init__(self, bot: VeritasBot):
        self.bot = bot

    def _guild(self) -> discord.Guild:
        guild = self.bot.home_guild
        if guild is None or not self.bot.is_ready():
            raise BridgeUnavailable("Бот ещё не подключился к серверу")
        return guild

    async def clearance(self, discord_id: int) -> ClearanceResponse:
        guild = self._guild()
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except discord.NotFound:
                member = None
        if member is None:
            return ClearanceResponse(discord_id=str(discord_id), member=False, level=0, roles=[],
                                     is_liar=await self.bot.db.is_liar(discord_id),
                                     is_exiled=await self.bot.db.is_exiled(discord_id))
        keys = self.bot.guild_map(guild).member_keys(member)
        return ClearanceResponse(
            discord_id=str(discord_id), member=True, level=clearance_level(keys),
            roles=[config.ROLE_SLUGS[k] for k in config.ROLE_ORDER if k in keys],
            is_liar=config.LIAR in keys, is_exiled=config.EXILE in keys)

    async def _post(self, key: str, embed: discord.Embed) -> int:
        guild = self._guild()
        channel = self.bot.guild_map(guild).channel(key)
        if channel is None:
            raise BridgeUnavailable(f"Канал «{key}» не найден — нужен /setup run")
        msg = await channel.send(embed=embed)
        return msg.id

    async def announce_archive(self, item: ArchiveAnnouncement) -> int:
        mid = await self._post("archive", archive_embed(item))
        self.bot.log_event("📁 Объявление архива", f"{dossier_code(item.number)} · {item.title} · гриф {item.level}")
        return mid

    async def announce_vault(self, item: VaultAnnouncement) -> int:
        mid = await self._post("vault", vault_embed(item))
        self.bot.log_event("🗝 Хранилище", f"{item.title} · гриф {item.level}")
        return mid


class Archive(commands.Cog):
    archive_group = app_commands.Group(name="archive", description="Архив Veritas", guild_only=True)

    def __init__(self, bot: VeritasBot):
        self.bot = bot
        self.bridge = BotBridge(bot)
        self._server = None
        self._server_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        s = self.bot.settings
        if not s.api_token:
            log.info("API_TOKEN не задан — HTTP API выключен")
            return
        try:
            app = create_app(self.bridge, s.api_token, s.allowed_site_domains)
        except ValueError as exc:
            log.error("API не запущен: %s", exc)
            return
        self._server = make_server(app, s.api_host, s.api_port)
        self._server_task = asyncio.create_task(self._serve(), name="veritas-api")
        log.info("API слушает %s:%s", s.api_host, s.api_port)

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except Exception:  # noqa: BLE001 — бот продолжает работать без API
            log.exception("HTTP API остановлен из-за ошибки")

    async def cog_unload(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()

    @archive_group.command(name="announce", description="Объявить новое досье в #архив")
    @app_commands.describe(number="Номер досье (42 → ДОСЬЕ-0042)", level="Гриф 2–5",
                           url="Ссылка на сайт", reliability="Достоверность", author="Автор (по умолчанию ты)")
    async def announce(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 999999],
                       title: str, level: app_commands.Range[int, 2, 5], url: str, reliability: str,
                       author: str | None = None) -> None:
        if not member_keys(interaction) & ARCHIVISTS:
            raise Denied("Объявления архива публикуют Архивариус и Совет.")
        try:
            item = ArchiveAnnouncement(number=number, title=title, level=level, url=url,
                                       reliability=reliability, author=author or interaction.user.display_name)
        except ValueError as exc:
            raise Denied(f"Проверь поля: {exc.errors()[0]['msg'] if hasattr(exc, 'errors') else exc}") from None
        error = url_error(item.url, self.bot.settings.allowed_site_domains) or secret_error(item.title)
        if error:
            raise Denied(error)
        try:
            await self.bridge.announce_archive(item)
        except BridgeUnavailable as exc:
            raise Denied(str(exc)) from None
        await interaction.response.send_message(f"Опубликовано: {dossier_code(number)}.", ephemeral=True)


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(Archive(bot))
