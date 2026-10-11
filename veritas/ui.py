"""Общие помощники интерфейса: проверки допуска, ответы об ошибках, безопасные View/Modal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import config

if TYPE_CHECKING:
    from .bot import VeritasBot

log = logging.getLogger("veritas.ui")


class Denied(app_commands.CheckFailure):
    """Отказ с понятным человеку текстом."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def explain_error(exc: BaseException) -> str:
    """Текст для человека по исключению."""
    original = getattr(exc, "original", None) or exc
    if isinstance(original, Denied):
        return original.message
    if isinstance(original, app_commands.CheckFailure):
        return "У тебя нет доступа к этой команде."
    if isinstance(original, discord.Forbidden):
        return ("Боту не хватает прав для этого действия. "
                "Проверь права бота и положение его роли в списке ролей.")
    if isinstance(original, discord.NotFound):
        return "Не нашёл нужный объект в Discord — возможно, его уже удалили."
    if isinstance(original, discord.HTTPException):
        return "Discord вернул ошибку. Попробуй ещё раз чуть позже."
    return "Что-то пошло не так. Совет уже видит ошибку в логах."


async def send_ephemeral(interaction: discord.Interaction, text: str, **kw) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kw)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kw)
    except discord.HTTPException:
        log.debug("Не удалось ответить на взаимодействие", exc_info=True)


async def report_error(interaction: discord.Interaction, exc: BaseException, where: str = "") -> None:
    """Ответить человеку понятным ephemeral-сообщением и записать ошибку в консоль и #логи."""
    original = getattr(exc, "original", None) or exc
    expected = isinstance(original, (app_commands.CheckFailure, discord.Forbidden, discord.NotFound))
    if not expected:
        log.error("Ошибка в %s", where or "взаимодействии", exc_info=original)
    await send_ephemeral(interaction, explain_error(exc))
    if not isinstance(original, app_commands.CheckFailure):
        bot = interaction.client
        logs = getattr(bot, "logs", None)
        if logs is not None:
            logs.error_event(where or "interaction", original, interaction.user)


class SafeView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_error(interaction, error, f"кнопка {getattr(item, 'custom_id', item)}")


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error, f"форма {self.title}")


# ───────────────────── ПРОВЕРКИ ДОПУСКА ─────────────────────


def member_keys(interaction: discord.Interaction) -> set[str]:
    bot: VeritasBot = interaction.client  # type: ignore[assignment]
    gm = bot.guild_map(interaction.guild)
    if gm is None or not isinstance(interaction.user, discord.Member):
        return set()
    return gm.member_keys(interaction.user)


def member_level(interaction: discord.Interaction) -> int:
    from .clearance import clearance_level

    return clearance_level(member_keys(interaction))


def ensure_level(interaction: discord.Interaction, level: int, text: str | None = None) -> None:
    if member_level(interaction) < level:
        raise Denied(text or "Твоего допуска недостаточно.")


def min_level(level: int, text: str | None = None):
    """Декоратор слэш-команды: требуемый уровень допуска."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise Denied("Команда работает только на сервере.")
        ensure_level(interaction, level, text)
        return True

    return app_commands.check(predicate)


def top_only():
    """Только Основатель (или владелец сервера) и Глава Совета."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise Denied("Команда работает только на сервере.")
        if not member_keys(interaction) & {config.FOUNDER, config.HEAD}:
            raise Denied("Это могут только Основатель и Глава Совета.")
        return True

    return app_commands.check(predicate)
