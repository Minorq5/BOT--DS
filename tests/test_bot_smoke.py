import discord

from veritas.bot import VeritasBot, make_intents
from veritas.config import Settings


def test_intents():
    i = make_intents()
    assert i.members and i.message_content and i.guilds and i.moderation
    assert not make_intents(message_content=False).message_content


async def test_bot_constructs_without_network(tmp_path):
    bot = VeritasBot(Settings(guild_id=1, db_path=str(tmp_path / "b.db")))
    assert isinstance(bot.intents, discord.Intents)
    assert bot.guild_map(None) is None
    await bot.db.close()
