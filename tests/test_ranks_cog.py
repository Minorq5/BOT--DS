import pytest

from veritas import config as C
from veritas.cogs.ranks import Ranks
from veritas.ui import Denied

from .fakes import FakeBot, FakeGuild, FakeMember, make_channel


@pytest.fixture
def env(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    court = make_channel("text", "⚖️┃суд-и-допуски")
    council = make_channel("text", "⚫┃совет")
    guild._channels += [court, council]
    bot.channel_ids.update(court=court.id, council=council.id)
    return bot, guild, Ranks(bot), court, council


async def test_liar_then_restore(env):
    bot, guild, cog, court, _ = env
    mod = FakeMember(guild, 1, [C.COUNCIL])
    target = FakeMember(guild, 2, [C.FOLL, C.INIT, C.TRUST])
    await cog.liar(guild, mod, target, "слив")
    assert target.keys() == {C.FOLL, C.LIAR}
    assert await bot.db.is_liar(2)
    court.send.assert_awaited()  # журнал
    target.send.assert_awaited()  # ЛС
    await cog.restore(guild, mod, target, "оправдан")
    assert target.keys() == {C.FOLL, C.INIT, C.TRUST}
    assert not await bot.db.is_liar(2)


async def test_exile_then_restore_and_rejoin_record(env):
    bot, guild, cog, *_ = env
    mod = FakeMember(guild, 1, [C.HEAD])
    target = FakeMember(guild, 3, [C.FOLL, C.INIT])
    await cog.exile(guild, mod, target, "суд")
    assert target.keys() == {C.EXILE}
    assert await bot.db.is_exiled(3)
    await cog.restore(guild, mod, target, "апелляция")
    assert target.keys() == {C.FOLL, C.INIT}
    assert not await bot.db.is_exiled(3)


async def test_liar_then_exile_then_restore_returns_everything(env):
    bot, guild, cog, *_ = env
    mod = FakeMember(guild, 1, [C.COUNCIL])
    target = FakeMember(guild, 4, [C.FOLL, C.TRUST])
    await cog.liar(guild, mod, target, "ложь")
    await cog.exile(guild, mod, target, "повторно")
    assert target.keys() == {C.EXILE}
    await cog.restore(guild, mod, target, "амнистия")
    assert target.keys() == {C.FOLL, C.TRUST}


@pytest.mark.parametrize("protected", [C.COUNCIL, C.HEAD, C.FOUNDER])
async def test_marks_refused_on_council_and_founder(env, protected):
    bot, guild, cog, *_ = env
    mod = FakeMember(guild, 1, [C.FOUNDER])
    target = FakeMember(guild, 5, [protected])
    for action in (cog.liar, cog.exile):
        with pytest.raises(Denied):
            await action(guild, mod, target, "x")
    target.remove_roles.assert_not_called()


async def test_owner_is_untouchable(env):
    bot, guild, cog, *_ = env
    mod = FakeMember(guild, 1, [C.HEAD])
    owner = FakeMember(guild, guild.owner_id, [C.FOLL])
    with pytest.raises(Denied):
        await cog.exile(guild, mod, owner, "x")


async def test_trusted_cannot_mark(env):
    bot, guild, cog, *_ = env
    with pytest.raises(Denied):
        await cog.liar(guild, FakeMember(guild, 1, [C.TRUST]), FakeMember(guild, 6, [C.FOLL]), "x")


async def test_restore_skips_deleted_roles(env):
    bot, guild, cog, *_ = env
    mod = FakeMember(guild, 1, [C.COUNCIL])
    target = FakeMember(guild, 7, [C.FOLL, C.INIT])
    await cog.liar(guild, mod, target, "x")
    guild.roles.remove(guild.role_by_key(C.INIT))  # роль удалили с сервера
    await cog.restore(guild, mod, target, "y")
    assert target.keys() == {C.FOLL}


async def test_promote_path_and_hint(env):
    bot, guild, cog, court, _ = env
    trusted = FakeMember(guild, 1, [C.TRUST])
    arch = FakeMember(guild, 2, [C.ARCH])
    newbie = FakeMember(guild, 8, [C.STRANGER])
    await cog.promote(guild, trusted, newbie, "анкета")
    assert newbie.keys() == {C.FOLL}
    with pytest.raises(Denied):
        await cog.promote(guild, trusted, newbie, "рано")  # Посвящённого даёт Архивариус+
    text = await cog.promote(guild, arch, newbie, "вклад")
    assert newbie.keys() == {C.INIT}
    assert "активных дней" in text.lower()


async def test_demote(env):
    bot, guild, cog, *_ = env
    council = FakeMember(guild, 1, [C.COUNCIL])
    t = FakeMember(guild, 9, [C.TRUST])
    await cog.demote(guild, council, t, "неактивен")
    assert t.keys() == {C.INIT}


async def test_trusted_timeout_capped_at_24h(env):
    bot, guild, cog, *_ = env
    trusted = FakeMember(guild, 1, [C.TRUST])
    t = FakeMember(guild, 10, [C.FOLL])
    await cog.timeout(guild, trusted, t, 60, "флуд")
    t.timeout.assert_awaited()
    with pytest.raises(Denied):
        await cog.timeout(guild, trusted, t, 24 * 60 + 1, "долго")


async def test_three_warns_hint_council(env):
    bot, guild, cog, court, council = env
    trusted = FakeMember(guild, 1, [C.TRUST])
    t = FakeMember(guild, 11, [C.FOLL])
    for _ in range(2):
        await cog.warn(guild, trusted, t, "x")
    council.send.assert_not_called()
    text = await cog.warn(guild, trusted, t, "x")
    assert "№3" in text and "Лестница" in text
    council.send.assert_awaited()


async def test_activity_counts_only_numbers(env):
    bot, guild, cog, *_ = env
    cog.activity.add(5, 0.0)
    cog.activity.add(5, 10.0)
    cog.activity.add(5, 86400.0)
    await cog.flush()
    cog.activity.add(5, 20.0)
    await cog.flush()
    assert await cog.active_days(5) == (2, 4)
    cols = [r["name"] for r in await bot.db.fetchall("PRAGMA table_info(activity)")]
    assert cols == ["user_id", "day", "messages"]
