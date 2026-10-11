"""Класс бота Veritas: загрузка модулей, persistent views, синхронизация команд."""

from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands

from . import __version__, config
from .audit import AuditCache
from .cloud_backup import DiscordBackup
from .config import Settings
from .db import Database
from .guildmap import GuildMap, resolve_role_ids
from .ui import report_error

log = logging.getLogger("veritas.bot")

EXTENSIONS: tuple[str, ...] = (
    "veritas.cogs.logs",
    "veritas.cogs.setup",
    "veritas.cogs.verification",
    "veritas.cogs.tickets",
    "veritas.cogs.antiraid",
    "veritas.cogs.ranks",
    "veritas.cogs.archive",
)


def make_intents(message_content: bool = True) -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = message_content
    return intents


class VeritasBot(commands.Bot):
    def __init__(self, settings: Settings, *, message_content: bool = True, db: Database | None = None):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=make_intents(message_content),
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True),
        )
        self.settings = settings
        self.version = __version__
        self.db = db or Database(settings.db_path)
        self.started_at = time.time()
        self.content_enabled = message_content
        self.channel_ids: dict[str, int] = {}
        self.role_id_cache: dict[str, int] = {}
        self.missing_roles: list[str] = []
        self.logs = None  # LogService, подключается модулем логов
        self.audit = AuditCache()
        self.cloud_backup: DiscordBackup | None = DiscordBackup(self) if settings.discord_backup else None
        self._bg_tasks: list[asyncio.Task] = []
        self.tree.on_error = self._on_tree_error

    # ───────────── запуск ─────────────

    async def setup_hook(self) -> None:
        if self.cloud_backup is not None:
            log.info("Резервная копия в Discord: %s", await self.cloud_backup.restore_if_missing())
            self._bg_tasks.append(asyncio.create_task(self.cloud_backup.run_loop(), name="veritas-backup"))
        await self.db.connect()
        await self.reload_ids()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info("Слэш-команды синхронизированы: %d", len(synced))
            except discord.HTTPException:
                log.exception("Не удалось синхронизировать слэш-команды")
        if self.settings.public_url and self.settings.keepalive_minutes:
            from .keepalive import keepalive_loop

            self._bg_tasks.append(asyncio.create_task(
                keepalive_loop(self.settings.public_url, self.settings.keepalive_minutes), name="veritas-keepalive"))
            log.info("Само-пинг %s каждые %d мин", self.settings.public_url, self.settings.keepalive_minutes)
        if not self.content_enabled:
            log.warning("Message Content Intent выключен: анти-спам по тексту и содержимое "
                        "сообщений в логах отключены. Включи его в Developer Portal.")

    async def close(self) -> None:
        if self.cloud_backup is not None and not self.is_closed() and self.db.conn is not None:
            # Последняя копия до закрытия соединения с Discord (Render шлёт SIGTERM при деплое).
            try:
                for cog in list(self.cogs.values()):
                    flush = getattr(cog, "flush", None)
                    if flush is not None:
                        await flush()
                await asyncio.wait_for(self.cloud_backup.save(), timeout=20)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось сохранить копию базы при остановке")
        for task in self._bg_tasks:
            task.cancel()
        try:
            await super().close()
        finally:
            await self.db.close()

    def health_status(self) -> dict:
        """Для /health: жив ли процесс и подключён ли Discord."""
        body = {"discord_ready": self.is_ready(), "uptime_s": int(time.time() - self.started_at)}
        if self.is_ready() and self.latency == self.latency:  # NaN до подключения
            body["latency_ms"] = round(self.latency * 1000)
        if self.cloud_backup is not None:
            body["backup_enabled"] = self.cloud_backup.enabled
            body["backup_saved_at"] = self.cloud_backup.last_saved_at
        return body

    async def on_ready(self) -> None:
        log.info("Вошёл как %s (id=%s), версия %s", self.user, getattr(self.user, "id", "?"), self.version)
        guild = self.home_guild
        if guild is None:
            log.error("Бот не находится на сервере GUILD_ID=%s. Пригласи его и проверь ID.",
                      self.settings.guild_id)
            return
        gm = self.guild_map(guild)
        if self.missing_roles:
            log.warning("Не найдены роли: %s", ", ".join(self.missing_roles))
        if gm is not None:
            self._warn_extra_admins(guild, gm)

    # ───────────── сервер, роли, каналы ─────────────

    @property
    def home_guild(self) -> discord.Guild | None:
        return self.get_guild(self.settings.guild_id) if self.settings.guild_id else None

    def is_home(self, guild: discord.abc.Snowflake | None) -> bool:
        return guild is not None and guild.id == self.settings.guild_id

    async def reload_ids(self) -> None:
        self.channel_ids = await self.db.managed_channels()
        cached = await self.db.settings_with_prefix("role_id.")
        self.role_id_cache = {k: int(v) for k, v in cached.items() if v and v.isdigit()}

    def guild_map(self, guild: discord.Guild | None = None) -> GuildMap | None:
        guild = guild or self.home_guild
        if guild is None or not self.is_home(guild):
            return None
        roles = [(r.id, r.name) for r in reversed(guild.roles)]
        ids, missing = resolve_role_ids(roles, cached=self.role_id_cache)
        self.missing_roles = missing
        return GuildMap(guild, ids, self.channel_ids)

    def log_event(self, title: str, description: str, *, color: str = "blue",
                  category: str = "bot") -> None:
        """Событие бота в #логи (тихо пропускается, если логи ещё не подключены)."""
        if self.logs is not None:
            self.logs.bot_event(title, description, color=color, category=category)

    def _warn_extra_admins(self, guild: discord.Guild, gm: GuildMap) -> None:
        founder = gm.role(config.FOUNDER)
        for role in guild.roles:
            if role.permissions.administrator and not role.managed and (founder is None or role.id != founder.id):
                text = f"У роли «{role.name}» есть «Администратор». Сними вручную, если это не Основатель."
                log.warning(text)
                if self.logs is not None:
                    self.logs.warn_event("Лишний «Администратор»", text)

    # ───────────── ошибки ─────────────

    async def _on_tree_error(self, interaction: discord.Interaction, error: Exception) -> None:
        name = interaction.command.qualified_name if interaction.command else "команда"
        await report_error(interaction, error, f"/{name}")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        # Одно плохое событие не должно ронять бота.
        log.exception("Ошибка в обработчике события %s", event_method)
