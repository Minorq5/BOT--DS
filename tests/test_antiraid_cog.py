from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from veritas import config as C
from veritas.cogs.antiraid import AntiRaid
from veritas.logservice import LogService

from .fakes import FakeBot, FakeGuild, FakeMember, make_channel


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    council = make_channel("text", "⚫┃совет")
    guild._channels.append(council)
    bot.channel_ids["council"] = council.id
    bot.logs = LogService(channel_getter=lambda: None)
    clock = Clock()
    cog = AntiRaid(bot, clock=clock)
    return bot, guild, cog, clock, council


def joiner(guild, uid, age_days=365):
    m = FakeMember(guild, uid, [], name=f"user{uid}")
    m.created_at = datetime.now(UTC) - timedelta(days=age_days)
    m.avatar = object()
    return m


async def test_raid_starts_lockdown_and_times_out_newcomers(env):
    bot, guild, cog, clock, council = env
    members = []
    for i in range(8):
        clock.t += 1
        m = joiner(guild, 100 + i)
        members.append(m)
        await cog.on_member_join(m)
    assert cog.state["active"] and cog.detector.active
    assert "INVITES_DISABLED" in guild.features
    assert guild.verification_level == discord.VerificationLevel.high
    council.send.assert_awaited()  # оповещение Совету с пингом
    assert council.send.await_args.kwargs["view"] is not None
    members[-1].timeout.assert_awaited()
    late = joiner(guild, 200)
    await cog.on_member_join(late)
    late.timeout.assert_awaited()
    assert 200 in cog.state["raiders"]
    saved = await bot.db.get_state("lockdown")
    assert saved["active"] is True

    text = await cog.lift_lockdown(guild, None)
    assert "снят" in text
    assert "INVITES_DISABLED" not in guild.features
    assert guild.verification_level == discord.VerificationLevel.low
    assert not cog.detector.active


async def test_auto_lift_after_quiet(env):
    bot, guild, cog, clock, council = env
    await cog.start_lockdown(guild, "тест", None)
    clock.t += cog.raid_cfg.auto_lift_minutes * 60 + 1
    assert cog.detector.should_auto_lift(clock())


async def test_young_account_gets_timeout(env):
    bot, guild, cog, clock, council = env
    m = joiner(guild, 300, age_days=0.1)
    m.avatar = None
    await cog.on_member_join(m)
    m.timeout.assert_awaited()
    assert not cog.state.get("active")


async def test_spam_exempts_trusted_and_punishes_followers(env):
    bot, guild, cog, clock, council = env
    general = make_channel("text", "💬┃общий-чат")

    def msg(member, text):
        m = MagicMock()
        m.author, m.guild, m.channel, m.content = member, guild, general, text
        m.raw_mentions, m.raw_role_mentions = [], []
        m.delete = AsyncMock()
        general.parent_id = None
        return m

    trusted = FakeMember(guild, 1, [C.TRUST])
    m1 = msg(trusted, "https://evil.com")
    await cog.on_message(m1)
    m1.delete.assert_not_called()

    foll = FakeMember(guild, 2, [C.FOLL])
    m2 = msg(foll, "заходи discord.gg/raid")
    await cog.on_message(m2)
    m2.delete.assert_awaited()
    foll.timeout.assert_not_called()  # первое нарушение — предупреждение
    clock.t += 10
    m3 = msg(foll, "https://evil.com")
    await cog.on_message(m3)
    foll.timeout.assert_awaited()
    assert foll.timeout.await_args.args[0] == timedelta(minutes=10)
    row = await bot.db.fetchone("SELECT kind FROM sanctions WHERE user_id = 2")
    assert row["kind"] == "timeout"


async def test_nuke_strips_dangerous_roles_below_bot_only(env):
    bot, guild, cog, clock, council = env
    council_role = guild.role_by_key(C.COUNCIL)
    council_role.permissions = discord.Permissions(ban_members=True, kick_members=True, manage_roles=True)
    nuker = FakeMember(guild, 50, [C.COUNCIL, C.FOLL])
    high = guild.role_by_key(C.HEAD)
    high.position = 100  # выше роли бота
    high.permissions = discord.Permissions(manage_channels=True)
    nuker.roles.append(high)
    for kind in ("channel_delete", "role_delete"):
        assert not await cog.record_destructive(guild, 50, kind)
    assert await cog.record_destructive(guild, 50, "ban")
    nuker.remove_roles.assert_awaited()
    removed = nuker.remove_roles.await_args.args
    assert council_role in removed and high not in removed
    assert guild.role_by_key(C.FOLL) not in removed  # без опасных прав — не трогаем
    nuker.timeout.assert_awaited()
    snap = await bot.db.fetchone("SELECT kind FROM role_snapshots WHERE user_id = 50")
    assert snap["kind"] == "nuke"


async def test_nuke_ignores_owner_and_bot(env):
    bot, guild, cog, clock, council = env
    FakeMember(guild, guild.owner_id, [C.COUNCIL])
    for _ in range(5):
        assert not await cog.record_destructive(guild, guild.owner_id, "channel_delete")
        assert not await cog.record_destructive(guild, bot.user.id, "channel_delete")


async def test_admin_alert_and_rollback(env):
    bot, guild, cog, clock, council = env
    role = guild.role_by_key(C.TRUST)
    before = SimpleNamespace(permissions=discord.Permissions(moderate_members=True))
    role.permissions = discord.Permissions(moderate_members=True, administrator=True)
    role.guild = guild
    await cog.on_guild_role_update(before, role)
    assert council.send.await_args.kwargs["view"] is not None
    text = await cog.rollback_role(guild, role.id, FakeMember(guild, 9, [C.COUNCIL]))
    assert "возвращены" in text
    assert role.permissions == discord.Permissions(moderate_members=True)


async def test_raid_config_persists_and_validates(env):
    from veritas.ui import Denied

    bot, guild, cog, clock, council = env
    text = await cog.set_param("raid.join_threshold", "5")
    assert "8 → 5" in text and cog.raid_cfg.join_threshold == 5
    cog2 = AntiRaid(bot)
    await cog2.load_config()
    assert cog2.raid_cfg.join_threshold == 5
    with pytest.raises(Denied):
        await cog.set_param("raid.join_threshold", "abc")
    with pytest.raises(Denied):
        await cog.set_param("raid.lockdown_action", "ban")
    with pytest.raises(Denied):
        await cog.set_param("nope", "1")
