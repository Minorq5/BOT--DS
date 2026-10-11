"""Все модули грузятся, команды уникальны и укладываются в лимиты Discord."""

import re

import discord

from veritas.bot import EXTENSIONS, VeritasBot
from veritas.config import Settings

NAME_RE = re.compile(r"^[-_\w]{1,32}$")


def walk(cmds):
    for c in cmds:
        yield c
        if isinstance(c, discord.app_commands.Group):
            yield from walk(c.commands)


async def test_all_extensions_load_and_commands_valid(tmp_path):
    bot = VeritasBot(Settings(guild_id=1, db_path=str(tmp_path / "x.db")))
    await bot._async_setup_hook()  # инициализация без входа в Discord
    await bot.db.connect()
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    top = bot.tree.get_commands()
    names = [c.name for c in top]
    assert len(names) == len(set(names))
    assert len(top) <= 100
    for c in walk(top):
        assert NAME_RE.match(c.name) and c.name == c.name.lower(), c.name
        assert 1 <= len(c.description) <= 100, c.name
    expected = {"setup", "panel", "status", "ticket", "logs", "lockdown", "raid", "rank", "liar", "exile",
                "restore", "timeout", "untimeout", "warn", "warns", "archive"}
    assert expected <= set(names)
    assert {"Verification", "Tickets", "Logs", "AntiRaid", "Ranks", "Archive", "SetupCog"} <= set(bot.cogs)
    assert bot.logs is not None
    for ext in reversed(EXTENSIONS):
        await bot.unload_extension(ext)
    await bot.close()


async def test_status_embed(tmp_path):
    from veritas.cogs.setup import status_embed

    bot = VeritasBot(Settings(guild_id=1, db_path=str(tmp_path / "y.db")))
    await bot.db.connect()
    e = await status_embed(bot)
    names = [f.name for f in e.fields]
    assert "Локдаун" in names and "Открытых тикетов" in names
    await bot.db.close()
