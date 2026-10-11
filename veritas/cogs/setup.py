"""/setup и /panel — настройка структуры сервера из Discord."""

from __future__ import annotations

import io
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


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(SetupCog(bot))
