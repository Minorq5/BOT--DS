import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from veritas.audit import AuditCache
from veritas.cogs.logs import Logs
from veritas.logic import logs as fmt
from veritas.logservice import LogItem, LogService

from .fakes import FakeBot, FakeGuild, make_channel

# ───── приватность

PRIVATE = {101, 102, 103}  # совет, суд, логи
TICKETS_CAT = 900


@pytest.mark.parametrize("kw,ok", [
    (dict(channel_id=5), True),
    (dict(channel_id=101), False),                              # ⚫┃совет
    (dict(channel_id=55, parent_id=102), False),                # ветка внутри суда
    (dict(channel_id=56, category_id=TICKETS_CAT), False),      # канал тикета
    (dict(channel_id=57, ticket_channel_ids=[57]), False),      # тикет по списку из БД
    (dict(channel_id=58, category_id=1), True),
])
def test_content_allowed(kw, ok):
    channel_id = kw.pop("channel_id")
    assert fmt.content_allowed(channel_id, private_ids=PRIVATE, ticket_category_id=TICKETS_CAT, **kw) is ok


def _text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}: {f.value}" for f in embed.fields]
    return "\n".join(parts)


def test_private_delete_has_no_content():
    e = fmt.message_delete("<#101>", "<@1>", 1, "секрет совета", ["https://x/y.png"], cached=True, private=True)
    assert "секрет" not in _text(e) and "y.png" not in _text(e)
    assert fmt.PRIVATE_NOTE in _text(e)
    assert e.colour.value == 0xE74C3C


def test_public_delete_has_content_and_attachments():
    e = fmt.message_delete("<#5>", "<@1>", 1, "привет", ["https://x/y.png"], cached=True, private=False)
    assert "привет" in _text(e) and "y.png" in _text(e)


def test_not_cached_delete():
    e = fmt.message_delete("<#5>", None, None, None, [], cached=False, private=False)
    assert "не в кэше" in _text(e)


def test_no_intent_delete():
    e = fmt.message_delete("<#5>", "<@1>", 1, "", [], cached=True, private=False, content_available=False)
    assert "Message Content" in _text(e)


def test_edit_private_hides_both_versions():
    e = fmt.message_edit("<#101>", "<@1>", 1, "старое", "новое", None, private=True)
    assert "старое" not in _text(e) and "новое" not in _text(e)
    e = fmt.message_edit("<#5>", "<@1>", 1, "старое", "новое", "https://discord.com/x", private=False)
    assert "старое" in _text(e) and "новое" in _text(e)


def test_member_join_flags_new_account():
    now = datetime(2026, 1, 10, tzinfo=UTC)
    e = fmt.member_join("<@1>", 1, now - timedelta(days=1), now, "`abc` от <@2>")
    t = _text(e)
    assert "🆕" in t and "abc" in t
    assert e.colour.value == 0x2ECC71


def test_roles_change_colors():
    assert fmt.roles_change("<@1>", 1, ["a"], [], None).colour.value == 0x2ECC71
    assert fmt.roles_change("<@1>", 1, [], ["a"], None).colour.value == 0xE74C3C
    assert fmt.roles_change("<@1>", 1, ["a"], ["b"], "<@2>").colour.value == 0xF1C40F


def test_role_update_flags_dangerous_perms():
    added, removed = fmt.perm_diff(0, discord.Permissions(administrator=True).value)
    assert added == ["administrator"] and removed == []
    e = fmt.role_update("Доверенный", added, removed, [], "<@3>")
    assert "опасные" in e.title and "Администратор" in _text(e)


def test_find_used_invite():
    assert fmt.find_used_invite({"a": 1, "b": 0}, {"a": 2, "b": 0}) == "a"
    assert fmt.find_used_invite({"a": 1, "b": 0}, {"a": 2, "b": 1}) is None
    assert fmt.find_used_invite({"a": 0, "b": 3}, {"b": 3}) == "a"  # одноразовое исчезло
    assert fmt.find_used_invite({}, {}) is None


# ───── очередь

class FakeLogChannel:
    def __init__(self, fail_times=0, status=429):
        self.sent = []
        self.fail_times = fail_times
        self.status = status

    async def send(self, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise discord.HTTPException(MagicMock(status=self.status, reason="x"), "rate limited")
        self.sent.append(kw)


async def test_batching_groups_up_to_ten_embeds():
    svc = LogService(channel_getter=lambda: None)
    for i in range(13):
        svc.send("bot", fmt.info(f"e{i}", "x"))
    first = await svc.next_batch(wait=0.01)
    second = await svc.next_batch(wait=0.01)
    assert len(first) == 10 and len(second) == 3


async def test_file_is_sent_separately():
    svc = LogService(channel_getter=lambda: None)
    svc.send("bot", fmt.info("a", "x"))
    svc.send_file(fmt.info("file", "x"), "t.txt", b"data")
    svc.send("bot", fmt.info("b", "x"))
    b1 = await svc.next_batch(wait=0.01)
    b2 = await svc.next_batch(wait=0.01)
    b3 = await svc.next_batch(wait=0.01)
    assert [i.embed.title for i in b1] == ["a"]
    assert b2[0].file_data == b"data"
    assert [i.embed.title for i in b3] == ["b"]


async def test_retries_on_429_without_losing_events():
    ch = FakeLogChannel(fail_times=2)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    svc = LogService(channel_getter=lambda: ch, sleep=fake_sleep)
    ok = await svc.deliver([LogItem(fmt.info("a", "x")), LogItem(fmt.info("b", "x"))])
    assert ok and len(ch.sent) == 1 and len(ch.sent[0]["embeds"]) == 2
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


async def test_non_retryable_error_gives_up():
    ch = FakeLogChannel(fail_times=10, status=403)
    svc = LogService(channel_getter=lambda: ch, sleep=AsyncMock())
    assert await svc.deliver([LogItem(fmt.info("a", "x"))]) is False


async def test_disabled_category_and_force():
    svc = LogService(channel_getter=lambda: None)
    assert svc.is_enabled("voice") is False  # по умолчанию выключено
    svc.send("voice", fmt.info("v", "x"))
    assert svc.queue.qsize() == 0
    svc.send("voice", fmt.info("v", "x"), force=True)
    assert svc.queue.qsize() == 1


async def test_overflow_is_counted_not_raised():
    svc = LogService(channel_getter=lambda: None, maxsize=2)
    for _ in range(5):
        svc.send("bot", fmt.info("x", "x"))
    assert svc.queue.qsize() == 2 and svc.dropped == 3


async def test_toggle_persists(db):
    bot = SimpleNamespace(db=db)
    svc = LogService(bot)
    await svc.set_enabled("messages", False)
    fresh = LogService(bot)
    await fresh.load_settings()
    assert fresh.is_enabled("messages") is False
    with pytest.raises(KeyError):
        await svc.set_enabled("nope", True)


# ───── аудит

def test_audit_cache_ttl_and_match():
    now = [100.0]
    cache = AuditCache(ttl=60, clock=lambda: now[0])
    cache.add("kick", 5, 1, "<@1>", "спам")
    assert cache.find("kick", 5).user == "<@1>"
    assert cache.find("ban", 5) is None
    assert cache.find(("ban", "kick"), 5) is not None
    now[0] = 161.0
    assert cache.find("kick", 5) is None


async def test_audit_lookup_uses_cache_without_api():
    cache = AuditCache()
    cache.add("member_role_update", 7, 1, "<@1>", None)
    guild = MagicMock()
    rec = await cache.lookup(guild, "member_role_update", 7, wait=0)
    assert rec.user == "<@1>"
    guild.audit_logs.assert_not_called()


async def test_audit_fetch_is_rate_limited():
    cache = AuditCache(min_interval=60)
    guild = MagicMock()

    def audit_logs(**kw):
        async def gen():
            if False:
                yield None
        return gen()

    guild.audit_logs = MagicMock(side_effect=audit_logs)
    await cache.lookup(guild, "kick", 1, wait=0)
    await cache.lookup(guild, "kick", 2, wait=0)
    assert guild.audit_logs.call_count == 1


# ───── модуль: не логирует сам себя и приватное

async def test_cog_skips_logs_channel_and_hides_private(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    bot.audit = AuditCache()
    bot.get_guild = lambda gid: guild
    council = make_channel("text", "⚫┃совет")
    logs = make_channel("text", "📊┃логи")
    general = make_channel("text", "💬┃общий-чат")
    guild._channels += [council, logs, general]
    bot.channel_ids.update(council=council.id, logs=logs.id, general=general.id)
    svc = LogService(channel_getter=lambda: None)
    cog = Logs(bot, svc)

    def payload(channel_id, content):
        msg = SimpleNamespace(author=SimpleNamespace(bot=False, mention="<@5>", id=5), content=content,
                              attachments=[])
        return SimpleNamespace(guild_id=guild.id, channel_id=channel_id, cached_message=msg)

    await cog.on_raw_message_delete(payload(logs.id, "лог"))
    assert svc.queue.qsize() == 0  # защита от петли
    await cog.on_raw_message_delete(payload(council.id, "тайна совета"))
    item = svc.queue.get_nowait()
    assert "тайна" not in _text(item.embed)
    await cog.on_raw_message_delete(payload(general.id, "обычное"))
    assert "обычное" in _text(svc.queue.get_nowait().embed)
    await asyncio.sleep(0)
