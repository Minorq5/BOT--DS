"""/setup и /panel — настройка структуры сервера из Discord."""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..setup_runner import SetupRunner, managed_messages, upsert_message
from ..ui import Denied, min_level, top_only

if TYPE_CHECKING:
    from ..bot import VeritasBot


async def send_report(interaction: discord.Interaction, text: str) -> None:
    if len(text) <= 1900:
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)
    else:
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename="setup-report.txt")
        await interaction.followup.send("Отчёт длинный — во вложении.", file=file, ephemeral=True)


class SetupCog(commands.Cog):
    setup_group = app_commands.Group(name="setup", description="Настройка структуры сервера",
                                     guild_only=True)

    def __init__(self, bot: VeritasBot):
        self.bot = bot

    async def _run(self, interaction: discord.Interaction, dry_run: bool) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        runner = SetupRunner(interaction.guild, self.bot.db, self.bot.settings, dry_run=dry_run)
        report = await runner.run()
        await self.bot.reload_ids()
        await send_report(interaction, report.text())
        if not dry_run and self.bot.logs is not None:
            self.bot.logs.bot_event("⚙️ Настройка сервера",
                                    f"{interaction.user.mention} запустил /setup run. "
                                    f"Изменений: {report.changes}, ошибок: {len(report.errors)}.")

    @setup_group.command(name="run", description="Применить структуру: роли, права, каналы, тексты")
    @top_only()
    async def run(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, dry_run=False)

    @setup_group.command(name="dry-run", description="Показать план изменений, ничего не меняя")
    @top_only()
    async def dry_run(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, dry_run=True)

    @app_commands.command(name="panel", description="Переопубликовать закреплённые тексты и панели")
    @app_commands.describe(which="Какой текст обновить (по умолчанию — все)")
    @min_level(5, "Панели обновляет Совет.")
    async def panel(self, interaction: discord.Interaction, which: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gm = self.bot.guild_map(interaction.guild)
        lines = []
        items = [m for m in managed_messages() if which in (None, m[0])]
        if not items:
            raise Denied("Нет такого текста.")
        for key, channel_key, builder in items:
            channel = gm.channel(channel_key) if gm else None
            if channel is None:
                lines.append(f"· «{key}»: канал не найден — сначала /setup run")
                continue
            lines.append(await upsert_message(self.bot.db, interaction.guild, key, channel,
                                              builder(self.bot.settings), dry_run=False))
        await send_report(interaction, "\n".join(lines))

    @panel.autocomplete("which")
    async def _panel_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=k, value=k) for k, _, _ in managed_messages() if current in k][:25]


    @app_commands.command(name="status", description="Состояние бота")
    @app_commands.guild_only()
    @min_level(5, "Состояние бота видит Совет.")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await status_embed(self.bot), ephemeral=True)


async def status_embed(bot: VeritasBot) -> discord.Embed:
    from ..embeds import make_embed
    from ..logic.verification import human_duration

    uptime = human_duration(time.time() - bot.started_at)
    latency = bot.latency * 1000 if bot.latency == bot.latency else 0  # NaN до подключения
    antiraid = bot.get_cog("AntiRaid")
    lockdown = antiraid.status_text() if antiraid else "модуль защиты не загружен"
    tickets = await bot.db.fetchval("SELECT COUNT(*) FROM tickets WHERE status = 'open'", (), 0)
    pending = await bot.db.fetchval("SELECT COUNT(*) FROM applications WHERE status = 'pending'", (), 0)
    logs = bot.logs
    queue = f"{logs.queue.qsize()} в очереди, потеряно {logs.dropped}" if logs else "не подключены"
    api = "выключен" if not bot.settings.api_token else f"{bot.settings.api_host}:{bot.settings.api_port}"
    return make_embed("📟 Состояние Veritas", None, color="blue", fields=[
        ("Версия", bot.version, True), ("Аптайм", uptime, True), ("Задержка", f"{latency:.0f} мс", True),
        ("Локдаун", lockdown, False),
        ("Открытых тикетов", str(tickets), True), ("Анкет ждут решения", str(pending), True),
        ("Message Content", "включён" if bot.content_enabled else "выключен", True),
        ("Логи", queue, True), ("API", api, True),
        ("Не найдены роли", ", ".join(bot.missing_roles) or "—", False)])


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(SetupCog(bot))
