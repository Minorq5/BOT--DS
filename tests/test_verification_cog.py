from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from veritas import config as C
from veritas.cogs.verification import AppDecisionButton, Verification, VerificationStore
from veritas.config import Settings
from veritas.logic.verification import ACCEPTED, PENDING, REJECTED, AlreadyDecided, Answers

from .fakes import FakeBot, FakeGuild, FakeMember, make_interaction, sent_text

ANS = Answers("Вейл", "от друга", "Хочу помогать архиву и узнавать правду о нашем мире.", "да")


@pytest.fixture
def env(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    cog = Verification(bot)
    bot.cogs["Verification"] = cog
    return bot, guild, cog


async def test_store_decide_is_atomic(db):
    store = VerificationStore(db)
    app_id = await store.create(1, ANS, [], 100.0)
    assert (await store.get(app_id))["status"] == PENDING
    assert await store.decide(app_id, "accept", 5, None, 101.0) == ACCEPTED
    with pytest.raises(AlreadyDecided):
        await store.decide(app_id, "reject", 6, "x", 102.0)
    row = await store.get(app_id)
    assert row["status"] == ACCEPTED and row["decided_by"] == 5


async def test_accept_gives_follower_and_removes_stranger(env):
    bot, guild, cog = env
    member = FakeMember(guild, 42, [C.STRANGER])
    mod = FakeMember(guild, 43, [C.TRUST])
    app_id = await cog.store.create(42, ANS, [], 100.0)
    res = await cog.decide(guild, app_id, "accept", mod, None)
    assert res.ok
    assert member.keys() == {C.FOLL}
    member.send.assert_awaited()  # ЛС с приветствием
    assert (await cog.store.get(app_id))["status"] == ACCEPTED
    assert any("принята" in e[0] for e in bot.events)
    # повторное нажатие безопасно
    again = await cog.decide(guild, app_id, "accept", mod, None)
    assert not again.ok and "уже решена" in again.message
    assert member.add_roles.await_count == 1


async def test_cannot_accept_member_who_left(env):
    bot, guild, cog = env
    mod = FakeMember(guild, 43, [C.TRUST])
    app_id = await cog.store.create(4242, ANS, [], 100.0)
    res = await cog.decide(guild, app_id, "accept", mod, None)
    assert not res.ok and "покинул" in res.message
    assert (await cog.store.get(app_id))["status"] == PENDING


async def test_reject_records_cooldown_and_dms(env):
    bot, guild, cog = env
    member = FakeMember(guild, 42, [C.STRANGER])
    mod = FakeMember(guild, 43, [C.COUNCIL])
    app_id = await cog.store.create(42, ANS, [], 100.0)
    res = await cog.decide(guild, app_id, "reject", mod, "мало подробностей")
    assert res.ok
    assert member.keys() == {C.STRANGER}
    dm = member.send.await_args.args[0]
    assert "мало подробностей" in dm and "1 дн" in dm
    attempts = await cog.store.attempts(42, 0)
    assert attempts[-1].result == REJECTED
    member.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    decision = await cog._decision(member, attempts[-1].ts + 3600)
    assert not decision.allowed


async def test_role_failure_reverts_to_pending(env):
    import discord

    bot, guild, cog = env
    member = FakeMember(guild, 42, [C.STRANGER])
    member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    mod = FakeMember(guild, 43, [C.TRUST])
    app_id = await cog.store.create(42, ANS, [], 100.0)
    with pytest.raises(discord.Forbidden):
        await cog.decide(guild, app_id, "accept", mod, None)
    assert (await cog.store.get(app_id))["status"] == PENDING


@pytest.mark.parametrize("keys,allowed", [
    ([C.STRANGER], False), ([C.FOLL], False), ([C.INIT], False),
    ([C.TRUST], True), ([C.ARCH], True), ([C.COUNCIL], True), ([C.FOLL, C.LIAR], False),
    ([C.TRUST, C.LIAR], False),
])
async def test_decision_buttons_require_trusted(env, keys, allowed):
    bot, guild, cog = env
    user = FakeMember(guild, 50, keys)
    inter = make_interaction(bot, user)
    ok = await AppDecisionButton("accept", 1).interaction_check(inter)
    assert ok is allowed
    if not allowed:
        assert "Доверенные" in sent_text(inter)


async def test_join_assigns_stranger_or_exile(env):
    bot, guild, cog = env
    newbie = FakeMember(guild, 60, [])
    await cog.on_member_join(newbie)
    assert newbie.keys() == {C.STRANGER}

    await bot.db.execute("INSERT INTO exiled (user_id, ts) VALUES (?, ?)", (61, 1.0))
    exiled = FakeMember(guild, 61, [])
    await cog.on_member_join(exiled)
    assert exiled.keys() == {C.EXILE}

    robot = FakeMember(guild, 62, [], bot=True)
    await cog.on_member_join(robot)
    robot.add_roles.assert_not_called()


async def test_restore_on_rejoin(env):
    bot, guild, cog = env
    app_id = await cog.store.create(70, ANS, [], 1.0)
    await cog.store.decide(app_id, "accept", 1, None, 2.0)
    m = FakeMember(guild, 70, [])
    await cog.on_member_join(m)
    assert m.keys() == {C.STRANGER}  # по умолчанию выключено
    bot.settings = Settings(guild_id=guild.id, restore_on_rejoin=True)
    m2 = FakeMember(guild, 70, [])
    await cog.on_member_join(m2)
    assert m2.keys() == {C.FOLL}


async def test_start_refuses_verified_and_exiled(env):
    from veritas.ui import Denied

    bot, guild, cog = env
    for keys, text in (([C.FOLL], "уже"), ([C.EXILE], "апелляции")):
        inter = make_interaction(bot, FakeMember(guild, 80 + len(keys), keys))
        with pytest.raises(Denied, match=text):
            await cog.start(inter)
    stranger = FakeMember(guild, 90, [C.STRANGER])
    stranger.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    inter = make_interaction(bot, stranger)
    await cog.start(inter)
    inter.response.send_modal.assert_awaited()


async def _submit_env(env, mode, age_days):
    from datetime import timedelta

    from .fakes import make_channel

    bot, guild, cog = env
    bot.settings = Settings(guild_id=guild.id, verify_mode=mode)
    ch = make_channel("text", "📋┃анкеты")
    guild._channels.append(ch)
    bot.channel_ids["applications"] = ch.id
    user = FakeMember(guild, 100, [C.STRANGER])
    user.created_at = datetime.now(UTC) - timedelta(days=age_days)
    inter = make_interaction(bot, user)
    await cog.submit(inter, ANS)
    return user, ch, inter


async def test_auto_mode_accepts_immediately(env):
    user, ch, inter = await _submit_env(env, "auto", 30)
    assert user.keys() == {C.FOLL}
    assert ch.send.await_args.kwargs.get("view") is None  # без кнопок, только запись


async def test_auto_mode_young_account_goes_manual(env):
    user, ch, inter = await _submit_env(env, "auto", 1)
    assert user.keys() == {C.STRANGER}
    assert ch.send.await_args.kwargs.get("view") is not None
    embed = ch.send.await_args.kwargs["embed"]
    assert any("⚠️" in f.value for f in embed.fields)


async def test_manual_mode_posts_with_buttons(env):
    user, ch, inter = await _submit_env(env, "manual", 30)
    assert user.keys() == {C.STRANGER}
    view = ch.send.await_args.kwargs["view"]
    ids = [getattr(i, "custom_id", None) or i.item.custom_id for i in view.children]
    assert any(i.startswith("veritas:app:accept:") for i in ids)
    assert "Доверенным" in sent_text(inter)


async def test_rejoined_liar_can_still_verify(env):
    bot, guild, cog = env
    m = FakeMember(guild, 95, [C.STRANGER, C.LIAR])
    m.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    inter = make_interaction(bot, m)
    await cog.start(inter)
    inter.response.send_modal.assert_awaited()
