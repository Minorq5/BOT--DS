import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from veritas import config as C
from veritas.cogs.tickets import Tickets, TicketStore
from veritas.config import TicketConfig
from veritas.logic.tickets import (
    TranscriptMessage,
    build_transcript,
    channel_name,
    check_limits,
    handler_roles,
    inactivity_action,
    slugify,
)
from veritas.ui import Denied

from .fakes import FakeBot, FakeGuild, FakeMember, make_channel, make_interaction

CFG = TicketConfig()


# ───── чистая логика

def test_handlers_by_type():
    assert handler_roles("access") == ["TRUST", "ARCH", C.COUNCIL, C.HEAD, C.FOUNDER]
    assert handler_roles("complaint") == [C.COUNCIL, C.HEAD, C.FOUNDER]
    assert handler_roles("question") == [C.COUNCIL, C.HEAD, C.FOUNDER]


def test_limits():
    assert check_limits([], "access", None, 100, CFG) == (True, "")
    ok, why = check_limits(["access"], "access", None, 100, CFG)
    assert not ok and "уже открыт" in why
    ok, why = check_limits(["access", "complaint", "question"], "question", None, 100, CFG)
    assert not ok
    ok, why = check_limits(["access", "complaint"], "question", None, 100, CFG)
    assert ok
    ok, why = check_limits([], "access", 100 - CFG.create_cooldown_s + 5, 100, CFG)
    assert not ok and "Подожди" in why
    assert check_limits([], "access", 100 - CFG.create_cooldown_s, 100, CFG)[0]


def test_channel_names():
    assert channel_name(42, "Вейл Тёмный") == "тикет-0042-вейл-тёмный"
    assert channel_name(7, "xX_Shadow_Xx!!") == "тикет-0007-xx-shadow-xx"
    assert channel_name(1, "🔥🔥") == "тикет-0001-user"
    assert len(slugify("a" * 100)) == 20


def test_inactivity():
    week = CFG.inactivity_close_s
    assert inactivity_action(0, False, week - CFG.inactivity_warn_before_s - 1, CFG) is None
    assert inactivity_action(0, False, week - CFG.inactivity_warn_before_s, CFG) == "warn"
    assert inactivity_action(0, True, week - 10, CFG) is None
    assert inactivity_action(0, True, week, CFG) == "close"


def test_transcript_format():
    msgs = [TranscriptMessage(datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "vail", 1, "привет\nвторая",
                              ["https://cdn.discordapp.com/x.png"], 1)]
    text = build_transcript({"Тикет": "№0001"}, msgs)
    assert "[2026-01-01 12:00:00 UTC] vail (1):" in text
    assert "    вторая" in text and "📎 https://cdn.discordapp.com/x.png" in text
    assert "Сообщений: 1" in text


# ───── хранилище

async def test_numbering_is_sequential(db):
    store = TicketStore(db)
    _, n1 = await store.reserve(1, "access", 1.0)
    tid2, n2 = await store.reserve(2, "question", 2.0)
    assert (n1, n2) == (1, 2)
    await store.drop(tid2)  # канал не создан — номер освобождается
    _, n3 = await store.reserve(3, "question", 3.0)
    assert n3 == 2


async def test_close_is_atomic(db):
    store = TicketStore(db)
    tid, _ = await store.reserve(1, "access", 1.0)
    await store.set_channel(tid, 555)
    assert await store.begin_close(tid, 9, "x", 2.0)
    assert not await store.begin_close(tid, 9, "x", 3.0)


# ───── модуль на подделках

@pytest.fixture
def env(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    cat = make_channel("category", "🎫 ТИКЕТЫ")
    logs = make_channel("text", "📊┃логи")
    guild._channels += [cat, logs]
    bot.channel_ids.update(cat_tickets=cat.id, logs=logs.id)
    cog = Tickets(bot)
    bot.cogs["Tickets"] = cog
    return bot, guild, cog, cat, logs


async def test_open_access_ticket_creates_private_channel(env):
    bot, guild, cog, cat, logs = env
    author = FakeMember(guild, 10, [C.FOLL], name="Вейл")
    inter = make_interaction(bot, author)
    await cog.open_ticket(inter, "access")
    kwargs = guild.create_text_channel.await_args.kwargs
    name = guild.create_text_channel.await_args.args[0]
    assert name == "тикет-0001-вейл"
    assert kwargs["category"] is cat
    ow = kwargs["overwrites"]
    assert ow[guild.default_role].view_channel is False
    assert ow[author].view_channel is True
    assert ow[guild.role_by_key("TRUST")].view_channel is True
    assert ow[guild.me].manage_channels is True
    assert guild.role_by_key(C.FOLL) not in ow
    channel = guild._channels[-1]
    assert (await cog.store.by_channel(channel.id))["number"] == 1
    # второй такой же — отказ
    with pytest.raises(Denied):
        await cog.open_ticket(make_interaction(bot, author), "access")


async def test_complaint_not_visible_to_trusted(env):
    bot, guild, cog, cat, logs = env
    author = FakeMember(guild, 11, [C.INIT])
    await cog.open_ticket(make_interaction(bot, author), "complaint")
    ow = guild.create_text_channel.await_args.kwargs["overwrites"]
    assert guild.role_by_key("TRUST") not in ow
    assert ow[guild.role_by_key(C.COUNCIL)].view_channel is True


async def test_exiled_cannot_open(env):
    bot, guild, cog, cat, logs = env
    with pytest.raises(Denied, match="апелляции"):
        await cog.open_ticket(make_interaction(bot, FakeMember(guild, 12, [C.EXILE])), "question")


async def test_close_sends_transcript_and_deletes_only_ticket_channel(env):
    bot, guild, cog, cat, logs = env
    author = FakeMember(guild, 13, [C.FOLL], name="Вейл")
    await cog.open_ticket(make_interaction(bot, author), "access")
    channel = guild._channels[-1]
    channel.history_messages = [SimpleNamespace(
        created_at=datetime(2026, 1, 1, tzinfo=UTC), author=SimpleNamespace(id=13, __str__=lambda s: "vail"),
        content="помогите", attachments=[], embeds=[])]
    closer = FakeMember(guild, 14, [C.TRUST])
    msg = await cog.close_ticket(guild, channel, closer, "решено", delete_delay=0)
    assert "закрыт" in msg
    await asyncio.gather(*cog._tasks)
    channel.delete.assert_awaited()
    sent = logs.send.await_args.kwargs
    assert isinstance(sent["file"], discord.File)
    author.send.assert_awaited()  # транскрипт в ЛС
    row = await cog.store.by_channel(channel.id)
    assert row["status"] == "closed" and row["close_reason"] == "решено"
    # повторное закрытие ничего не делает
    assert "уже" in await cog.close_ticket(guild, channel, closer, None, delete_delay=0)


async def test_never_deletes_foreign_channel(env):
    bot, guild, cog, cat, logs = env
    other = make_channel("text", "💬┃общий-чат")
    guild._channels.append(other)
    await cog._delete_ticket_channel(guild, other)
    other.delete.assert_not_called()
    # даже если запись в БД есть, но канал вне категории тикетов
    tid, _ = await cog.store.reserve(1, "access", 1.0)
    await cog.store.set_channel(tid, other.id)
    await cog._delete_ticket_channel(guild, other)
    other.delete.assert_not_called()


async def test_only_author_or_handler_can_close(env):
    bot, guild, cog, cat, logs = env
    author = FakeMember(guild, 15, [C.FOLL])
    await cog.open_ticket(make_interaction(bot, author), "complaint")
    channel = guild._channels[-1]
    stranger = FakeMember(guild, 16, [C.FOLL])
    inter = make_interaction(bot, stranger)
    inter.channel = channel
    with pytest.raises(Denied):
        await cog.require_closer(inter)
    trusted = make_interaction(bot, FakeMember(guild, 17, [C.TRUST]))
    trusted.channel = channel
    with pytest.raises(Denied):  # жалобы — только Совет
        await cog.require_closer(trusted)
    own = make_interaction(bot, author)
    own.channel = channel
    assert (await cog.require_closer(own))["user_id"] == 15


# ───── приём материалов

def test_materials_handlers_only_head_and_founder():
    from veritas.logic.tickets import TICKET_TYPES, is_handler

    assert handler_roles("materials") == [C.HEAD, C.FOUNDER]
    assert is_handler({C.HEAD}, "materials") and is_handler({C.FOUNDER}, "materials")
    assert not is_handler({C.COUNCIL}, "materials") and not is_handler({C.ARCH}, "materials")
    assert is_handler({C.COUNCIL}, "complaint") and not is_handler({C.TRUST}, "complaint")
    assert is_handler({C.TRUST}, "access") and not is_handler({C.TRUST, C.LIAR}, "access")
    assert not TICKET_TYPES["materials"].on_panel  # не на панели #заявки


def test_materials_not_on_requests_panel():
    from veritas.cogs.tickets import TicketPanelView

    ids = [i.custom_id for i in TicketPanelView().children]
    assert "veritas:ticket:open:materials" not in ids and len(ids) == 3


async def test_open_materials_ticket_pings_head_and_founder(env):
    bot, guild, cog, cat, logs = env
    author = FakeMember(guild, 20, [C.INIT], name="Вейл")
    await cog.open_ticket(make_interaction(bot, author), "materials")
    name = guild.create_text_channel.await_args.args[0]
    assert name == "материал-0001-вейл"
    ow = guild.create_text_channel.await_args.kwargs["overwrites"]
    assert ow[guild.role_by_key(C.HEAD)].view_channel is True
    assert ow[guild.role_by_key(C.FOUNDER)].view_channel is True
    assert guild.role_by_key(C.COUNCIL) not in ow and guild.role_by_key("TRUST") not in ow
    channel = guild._channels[-1]
    first, second = channel.send.await_args_list
    intro = first.args[0]
    assert guild.role_by_key(C.HEAD).mention in intro and guild.role_by_key(C.FOUNDER).mention in intro
    assert first.kwargs["allowed_mentions"].roles is True
    assert "Предоставьте всю информацию" in second.args[0]


async def test_materials_pings_owner_when_no_founder_role(env):
    bot, guild, cog, cat, logs = env
    guild.roles.remove(guild.role_by_key(C.FOUNDER))
    await cog.open_ticket(make_interaction(bot, FakeMember(guild, 21, [C.INIT])), "materials")
    intro = guild._channels[-1].send.await_args_list[0].args[0]
    assert f"<@{guild.owner_id}>" in intro


async def test_council_cannot_handle_materials(env):
    bot, guild, cog, cat, logs = env
    await cog.open_ticket(make_interaction(bot, FakeMember(guild, 22, [C.INIT])), "materials")
    channel = guild._channels[-1]
    council = make_interaction(bot, FakeMember(guild, 23, [C.COUNCIL]))
    council.channel = channel
    with pytest.raises(Denied):
        await cog.require_handler(council)
    head = make_interaction(bot, FakeMember(guild, 24, [C.HEAD]))
    head.channel = channel
    assert (await cog.require_handler(head))["type"] == "materials"


async def test_mainez_turns_old_intake_message_into_panel(env):
    bot, guild, cog, cat, logs = env
    intake = make_channel("text", "📥┃приём-материалов")
    guild._channels.append(intake)
    old = await intake.send("старый шаблон")
    await bot.db.save_managed_message("intake", intake.id, old.id)
    inter = make_interaction(bot, FakeMember(guild, 30, [C.HEAD]))
    inter.channel = intake
    await cog.mainez.callback(cog, inter)
    old.edit.assert_awaited()
    kwargs = old.edit.await_args.kwargs
    assert kwargs["embeds"][0].title == "📥 Приём материалов"
    assert "награда" in kwargs["embeds"][0].description
    button = kwargs["view"].children[0]
    assert button.custom_id == "veritas:ticket:open:materials"
    assert len(intake.messages) == 1  # не дублирует
