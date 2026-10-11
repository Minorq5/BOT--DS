"""Минимальные подделки объектов Discord для тестов без сети."""

from __future__ import annotations

import functools
import itertools
from unittest.mock import AsyncMock, MagicMock

import discord

from veritas import config as C

_ids = itertools.count(10_000)


def next_id() -> int:
    return next(_ids)


@functools.total_ordering
class FakeRole:
    def __init__(self, id, name, position, permissions=None, managed=False):
        self.id = id
        self.name = name
        self.position = position
        self.permissions = permissions or discord.Permissions.none()
        self.managed = managed
        self.mention = f"<@&{id}>"
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, permissions=None, **kw):
        if permissions is not None:
            self.permissions = permissions

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    def __lt__(self, other):
        return self.position < other.position

    def __hash__(self):
        return hash(self.id)

    def is_default(self):
        return self.name == "@everyone"


class FakeMessage:
    def __init__(self, channel, content=None, embeds=None, view=None):
        self.id = next_id()
        self.channel = channel
        self.content = content
        self.embeds = embeds or []
        self.view = view
        self.pinned = False
        self.pin = AsyncMock(side_effect=self._pin)
        self.edit = AsyncMock()

    async def _pin(self, **kw):
        self.pinned = True


def make_channel(kind, name, category=None, overwrites=None, topic="", slowmode_delay=0):
    cls = {"text": discord.TextChannel, "news": discord.TextChannel, "voice": discord.VoiceChannel,
           "forum": discord.ForumChannel, "category": discord.CategoryChannel}[kind]
    ch = MagicMock(spec=cls)
    ch.id = next_id()
    ch.name = name
    ch.category_id = category.id if category is not None else None
    ch.overwrites = dict(overwrites or {})
    ch.topic = topic
    ch.slowmode_delay = slowmode_delay
    ch.available_tags = []
    ch.channels = []
    ch.is_news = MagicMock(return_value=kind == "news")
    ch.messages = {}

    async def send(content=None, embeds=None, view=None, **kw):
        m = FakeMessage(ch, content, embeds, view)
        ch.messages[m.id] = m
        return m

    async def fetch_message(mid):
        if mid not in ch.messages:
            raise discord.NotFound(MagicMock(status=404), "not found")
        return ch.messages[mid]

    async def edit(**kw):
        if "overwrites" in kw:
            ch.overwrites = dict(kw["overwrites"])
        if "slowmode_delay" in kw:
            ch.slowmode_delay = kw["slowmode_delay"]
        if "topic" in kw:
            ch.topic = kw["topic"]
        if "available_tags" in kw:
            ch.available_tags = kw["available_tags"]

    ch.send = AsyncMock(side_effect=send)
    ch.fetch_message = AsyncMock(side_effect=fetch_message)
    ch.edit = AsyncMock(side_effect=edit)
    ch.set_permissions = AsyncMock()
    ch.delete = AsyncMock()
    return ch


class FakeMe:
    def __init__(self, uid, top_role):
        self.id = uid
        self.top_role = top_role
        self.mention = f"<@{uid}>"
        self.guild_permissions = discord.Permissions.all()
        self.roles = [top_role]

    def __hash__(self):
        return hash(self.id)


class FakeGuild:
    def __init__(self, *, with_follower=True, community=False, bot_position=50):
        self.id = 1
        self.name = "Veritas Test"
        self.owner_id = 777
        self.features = ["COMMUNITY"] if community else []
        self.default_role = FakeRole(self.id, "@everyone", 0)
        roles = [self.default_role]
        pos = 1
        for key in reversed(C.ROLE_ORDER):
            if key == C.FOLL and not with_follower:
                continue
            rid = C.ROLE_IDS.get(key) or next_id()
            roles.append(FakeRole(rid, C.ROLE_TITLES[key], pos))
            pos += 1
        self.bot_role = FakeRole(next_id(), "Veritas Bot", bot_position, managed=True)
        roles.append(self.bot_role)
        self.roles = sorted(roles, key=lambda r: r.position)
        self.me = FakeMe(999, self.bot_role)
        self._channels: list = []
        self.create_role = AsyncMock(side_effect=self._create_role)
        self.create_category = AsyncMock(side_effect=self._create_category)
        self.create_text_channel = AsyncMock(side_effect=self._create_text)
        self.create_voice_channel = AsyncMock(side_effect=self._create_voice)
        self.create_forum = AsyncMock(side_effect=self._create_forum)
        self.edit_role_positions = AsyncMock()
        self.members: dict[int, object] = {}

    # доступ
    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return next((c for c in self._channels if c.id == cid), None)

    def get_member(self, uid):
        return self.members.get(uid)

    @property
    def channels(self):
        return list(self._channels)

    @property
    def categories(self):
        return [c for c in self._channels if isinstance(c, discord.CategoryChannel)]

    def role_by_key(self, key):
        if key in C.ROLE_IDS:
            return self.get_role(C.ROLE_IDS[key])
        return next((r for r in self.roles if r.name == C.ROLE_TITLES[key]), None)

    # создание
    async def _create_role(self, name, **kw):
        r = FakeRole(next_id(), name, 1)
        self.roles.insert(1, r)
        return r

    def _add(self, ch, category):
        self._channels.append(ch)
        if category is not None:
            category.channels.append(ch)
        return ch

    async def _create_category(self, name, overwrites=None, **kw):
        return self._add(make_channel("category", name, overwrites=overwrites), None)

    async def _create_text(self, name, category=None, overwrites=None, topic="", slowmode_delay=0,
                           news=False, **kw):
        kind = "news" if news else "text"
        return self._add(make_channel(kind, name, category, overwrites, topic, slowmode_delay), category)

    async def _create_voice(self, name, category=None, overwrites=None, **kw):
        return self._add(make_channel("voice", name, category, overwrites), category)

    async def _create_forum(self, name, category=None, overwrites=None, topic="", slowmode_delay=0,
                            available_tags=(), **kw):
        ch = make_channel("forum", name, category, overwrites, topic, slowmode_delay)
        ch.available_tags = list(available_tags)
        return self._add(ch, category)


class FakeMember:
    def __init__(self, guild, uid, role_keys=(), *, bot=False, name="user"):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.name = name
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.roles = [guild.default_role] + [guild.role_by_key(k) for k in role_keys if guild.role_by_key(k)]
        self.add_roles = AsyncMock(side_effect=self._add_roles)
        self.remove_roles = AsyncMock(side_effect=self._remove_roles)
        self.timeout = AsyncMock()
        self.kick = AsyncMock()
        self.send = AsyncMock()
        self.created_at = None
        self.joined_at = None
        self.avatar = None
        self.top_role = max(self.roles, key=lambda r: r.position)
        guild.members[uid] = self

    async def _add_roles(self, *roles, **kw):
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def _remove_roles(self, *roles, **kw):
        for r in roles:
            if r in self.roles:
                self.roles.remove(r)

    def keys(self):
        result = set()
        for key in C.ROLE_ORDER:
            r = self.guild.role_by_key(key)
            if r is not None and r in self.roles:
                result.add(key)
        return result
