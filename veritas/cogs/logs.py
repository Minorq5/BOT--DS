"""Логи сервера в #логи: участники, роли, сообщения, сервер, голос, события бота."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..embeds import utcnow
from ..logic import logs as fmt
from ..logservice import LogService
from ..ui import min_level

if TYPE_CHECKING:
    from ..bot import VeritasBot

log = logging.getLogger("veritas.logs")

CATEGORY_CHOICES = [app_commands.Choice(name=n, value=n) for n in config.LOG_CATEGORIES]
CATEGORY_TITLES = {"members": "участники", "roles": "роли участников", "messages": "сообщения",
                   "server": "сервер", "voice": "голос", "bot": "события бота"}


def _channel_kind(ch) -> str:
    return {"voice": "голосовой", "forum": "форум", "category": "категория", "news": "объявления"}.get(
        str(getattr(ch, "type", "text")), "текстовый")


class Logs(commands.Cog):
    logs_group = app_commands.Group(name="logs", description="Логи сервера", guild_only=True)

    def __init__(self, bot: VeritasBot, service: LogService | None = None):
        self.bot = bot
        self.service = service or LogService(bot)
        bot.logs = self.service
        self.invites: dict[str, int] | None = None
        self._recent_bans: dict[int, float] = {}

    async def cog_load(self) -> None:
        await self.service.start()

    async def cog_unload(self) -> None:
        await self.service.stop()
        self.bot.logs = None

    # ── помощники

    def _home(self, guild) -> bool:
        return self.bot.is_home(guild)

    def _send(self, category: str, embed: discord.Embed) -> None:
        self.service.send(category, embed)

    def _logs_channel_id(self) -> int | None:
        return self.bot.channel_ids.get("logs")

    def content_ok(self, channel_id: int, guild: discord.Guild | None) -> bool:
        ch = guild.get_channel_or_thread(channel_id) if guild is not None else None
        parent_id = getattr(ch, "parent_id", None)
        category_id = getattr(ch, "category_id", None)
        if parent_id is not None and category_id is None and guild is not None:
            parent = guild.get_channel(parent_id)
            category_id = getattr(parent, "category_id", None)
        private = {self.bot.channel_ids[k] for k in config.PRIVATE_CHANNEL_KEYS if k in self.bot.channel_ids}
        return fmt.content_allowed(channel_id, parent_id=parent_id, category_id=category_id, private_ids=private,
                                   ticket_category_id=self.bot.channel_ids.get("cat_tickets"))

    def _is_ticket_channel(self, ch) -> bool:
        tickets = self.bot.channel_ids.get("cat_tickets")
        return tickets is not None and getattr(ch, "category_id", None) == tickets

    async def _actor(self, guild, actions, target_id) -> tuple[str | None, str | None]:
        try:
            rec = await self.bot.audit.lookup(guild, actions, target_id)
        except Exception:  # noqa: BLE001 — журнал аудита не критичен
            log.debug("lookup failed", exc_info=True)
            rec = None
        return (rec.user, rec.reason) if rec else (None, None)

    # ── аудит и приглашения

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if self._home(entry.guild):
            self.bot.audit.add_entry(entry)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = self.bot.home_guild
        if guild is not None:
            await self._refresh_invites(guild)

    async def _refresh_invites(self, guild: discord.Guild) -> dict[str, discord.Invite]:
        if not guild.me.guild_permissions.manage_guild:
            self.invites = None
            return {}
        try:
            invites = {i.code: i for i in await guild.invites()}
        except discord.HTTPException:
            self.invites = None
            return {}
        self.invites = {code: inv.uses or 0 for code, inv in invites.items()}
        return invites

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if not self._home(invite.guild):
            return
        if self.invites is not None:
            self.invites[invite.code] = invite.uses or 0
        inviter = invite.inviter.mention if invite.inviter else "неизвестно"
        limits = []
        if invite.max_uses:
            limits.append(f"использований: {invite.max_uses}")
        if invite.max_age:
            limits.append(f"срок: {invite.max_age // 3600} ч")
        self._send("server", fmt.server_change("🔗 Приглашение создано", fmt.GREEN, [
            f"`{invite.code}` в {getattr(invite.channel, 'mention', '?')}", ", ".join(limits)], inviter))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if self._home(invite.guild):
            self._send("server", fmt.server_change("🔗 Приглашение удалено", fmt.RED, [f"`{invite.code}`"]))

    async def _detect_invite(self, guild: discord.Guild) -> str | None:
        if self.invites is None:
            return None
        before = dict(self.invites)
        current = await self._refresh_invites(guild)
        code = fmt.find_used_invite(before, self.invites or {})
        if code is None:
            return None
        inv = current.get(code)
        who = inv.inviter.mention if inv and inv.inviter else "неизвестно"
        return f"`{code}` от {who}"

    # ── участники

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._home(member.guild):
            return
        flags = []
        if member.bot:
            flags.append("🤖 бот")
        if member.avatar is None:
            flags.append("аватар по умолчанию")
        inviter = await self._detect_invite(member.guild)
        self._send("members", fmt.member_join(member.mention, member.id, member.created_at, utcnow(),
                                              inviter, flags))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if not self._home(guild):
            return
        user = payload.user
        if time.monotonic() - self._recent_bans.pop(user.id, -1e9) < 60:
            return  # бан уже залогирован
        rec = self.bot.audit.find("ban", user.id, within=15)
        if rec is not None:
            return
        actor, reason = await self._actor(guild, "kick", user.id)
        if user.id in self._recent_bans or self.bot.audit.find("ban", user.id, within=15) is not None:
            return  # событие бана пришло позже выхода — его залогирует on_member_ban
        if actor is not None:
            self._send("members", fmt.moderation("👢 Кик", user.mention, user.id, actor, reason))
            return
        roles = [r.name for r in getattr(user, "roles", []) if not r.is_default()]
        self._send("members", fmt.member_leave(user.mention, user.id, getattr(user, "joined_at", None), roles))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._home(guild):
            return
        self._recent_bans[user.id] = time.monotonic()
        actor, reason = await self._actor(guild, "ban", user.id)
        self._send("members", fmt.moderation("🔨 Бан", user.mention, user.id, actor, reason))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._home(guild):
            return
        actor, reason = await self._actor(guild, "unban", user.id)
        self._send("members", fmt.moderation("🕊 Разбан", user.mention, user.id, actor, reason, color=fmt.GREEN))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._home(after.guild):
            return
        if before.nick != after.nick:
            self._send("members", fmt.nick_change(after.mention, after.id, before.nick, after.nick))
        if before.timed_out_until != after.timed_out_until:
            actor, reason = await self._actor(after.guild, "member_update", after.id)
            until = after.timed_out_until if after.is_timed_out() else None
            self._send("members", fmt.timeout_change(after.mention, after.id, until, actor, reason))
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            actor, _ = await self._actor(after.guild, "member_role_update", after.id)
            self._send("roles", fmt.roles_change(after.mention, after.id, [r.mention for r in added],
                                                 [r.mention for r in removed], actor))

    # ── сообщения

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self._home(guild) or payload.channel_id == self._logs_channel_id():
            return
        msg = payload.cached_message
        if msg is not None and msg.author.bot:
            return
        self._send("messages", fmt.message_delete(
            f"<#{payload.channel_id}>",
            msg.author.mention if msg else None, msg.author.id if msg else None,
            msg.content if msg else None, [a.url for a in msg.attachments] if msg else [],
            cached=msg is not None, private=not self.content_ok(payload.channel_id, guild),
            content_available=self.bot.content_enabled))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self._home(guild) or payload.channel_id == self._logs_channel_id():
            return
        self._send("messages", fmt.bulk_delete(f"<#{payload.channel_id}>", len(payload.message_ids)))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self._home(guild) or payload.channel_id == self._logs_channel_id():
            return
        after = payload.message
        if after is None or after.author.bot or not payload.data.get("edited_timestamp"):
            return  # правки ботов и подгрузка превью ссылок — не правки человека
        before = payload.cached_message
        if before is not None and before.content == after.content:
            return
        self._send("messages", fmt.message_edit(
            f"<#{payload.channel_id}>", after.author.mention, after.author.id,
            before.content if before is not None else None, after.content, after.jump_url,
            private=not self.content_ok(payload.channel_id, guild), content_available=self.bot.content_enabled))

    # ── сервер

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch: discord.abc.GuildChannel) -> None:
        if not self._home(ch.guild) or self._is_ticket_channel(ch):
            return
        actor, _ = await self._actor(ch.guild, "channel_create", ch.id)
        self._send("server", fmt.server_change("➕ Канал создан", fmt.GREEN, [
            f"{ch.mention} · {_channel_kind(ch)}", f"Категория: {ch.category.name}" if ch.category else ""], actor))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch: discord.abc.GuildChannel) -> None:
        if not self._home(ch.guild) or self._is_ticket_channel(ch):
            return
        actor, _ = await self._actor(ch.guild, "channel_delete", ch.id)
        self._send("server", fmt.server_change("🗑 Канал удалён", fmt.RED, [f"#{ch.name} · {_channel_kind(ch)}"],
                                               actor))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        if not self._home(after.guild) or self._is_ticket_channel(after):
            return
        lines = []
        if before.name != after.name:
            lines.append(f"Название: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            lines.append("Описание изменено")
        if getattr(before, "slowmode_delay", 0) != getattr(after, "slowmode_delay", 0):
            lines.append(f"Медленный режим: {before.slowmode_delay} → {after.slowmode_delay} с")
        if before.category_id != after.category_id:
            lines.append(f"Категория: {getattr(before.category, 'name', '—')} → {getattr(after.category, 'name', '—')}")
        if before.overwrites != after.overwrites:
            changed = [t for t in set(before.overwrites) | set(after.overwrites)
                       if before.overwrites.get(t) != after.overwrites.get(t)]
            names = ", ".join(getattr(t, "name", str(t.id)) for t in changed[:10])
            lines.append(f"Права изменены для: {names}")
        if not lines:
            return
        actor, _ = await self._actor(after.guild, ("channel_update", "overwrite_update", "overwrite_create",
                                                   "overwrite_delete"), after.id)
        self._send("server", fmt.server_change(f"✏️ Канал изменён: #{after.name}", fmt.YELLOW, lines, actor))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if not self._home(role.guild):
            return
        actor, _ = await self._actor(role.guild, "role_create", role.id)
        added, _ = fmt.perm_diff(0, role.permissions.value)
        self._send("server", fmt.server_change("➕ Роль создана", fmt.GREEN, [
            role.mention, ("Права: " + ", ".join(fmt.perm_name(p) for p in added)) if added else ""], actor))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if not self._home(role.guild):
            return
        actor, _ = await self._actor(role.guild, "role_delete", role.id)
        self._send("server", fmt.server_change("🗑 Роль удалена", fmt.RED, [f"@{role.name}"], actor))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if not self._home(after.guild):
            return
        added, removed = fmt.perm_diff(before.permissions.value, after.permissions.value)
        other = []
        if before.name != after.name:
            other.append(f"Название: `{before.name}` → `{after.name}`")
        if before.colour != after.colour:
            other.append(f"Цвет: {before.colour} → {after.colour}")
        if before.hoist != after.hoist or before.mentionable != after.mentionable:
            other.append("Отображение/упоминание изменено")
        if not (added or removed or other):
            return  # например, сдвиг позиции
        actor, _ = await self._actor(after.guild, "role_update", after.id)
        self._send("server", fmt.role_update(after.name, added, removed, other, actor))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after) -> None:
        if not self._home(guild):
            return
        b, a = {e.id: e for e in before}, {e.id: e for e in after}
        lines = [f"➕ {a[i]} `:{a[i].name}:`" for i in a.keys() - b.keys()]
        lines += [f"➖ `:{b[i].name}:`" for i in b.keys() - a.keys()]
        lines += [f"✏️ `:{b[i].name}:` → `:{a[i].name}:`" for i in a.keys() & b.keys() if a[i].name != b[i].name]
        if lines:
            self._send("server", fmt.server_change("😀 Эмодзи", fmt.YELLOW, lines))

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        if not self._home(channel.guild):
            return
        try:
            rec = await self.bot.audit.lookup(channel.guild, ("webhook_create", "webhook_update", "webhook_delete"),
                                              None)
        except Exception:  # noqa: BLE001
            rec = None
        what = {"webhook_create": "создан", "webhook_update": "изменён", "webhook_delete": "удалён"}.get(
            rec.action if rec else "", "изменены")
        self._send("server", fmt.server_change("🪝 Вебхук " + what, fmt.RED if what == "создан" else fmt.YELLOW,
                                               [f"Канал: {channel.mention}"], rec.user if rec else None))

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if not self._home(after):
            return
        lines = []
        if before.name != after.name:
            lines.append(f"Название: `{before.name}` → `{after.name}`")
        if before.verification_level != after.verification_level:
            lines.append(f"Уровень проверки: {before.verification_level} → {after.verification_level}")
        if before.explicit_content_filter != after.explicit_content_filter:
            lines.append("Фильтр контента изменён")
        if before.owner_id != after.owner_id:
            lines.append(f"Владелец: <@{before.owner_id}> → <@{after.owner_id}>")
        added = set(after.features) - set(before.features)
        removed = set(before.features) - set(after.features)
        if added:
            lines.append("➕ " + ", ".join(sorted(added)))
        if removed:
            lines.append("➖ " + ", ".join(sorted(removed)))
        if lines:
            actor, _ = await self._actor(after, "guild_update", after.id)
            self._send("server", fmt.server_change("⚙️ Настройки сервера", fmt.YELLOW, lines, actor))

    # ── голос

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if not self._home(member.guild) or before.channel == after.channel or not self.service.is_enabled("voice"):
            return
        if before.channel is None:
            embed = fmt.info("🎙 Вход в голосовой", f"{member.mention} → {after.channel.mention}", fmt.GREEN)
        elif after.channel is None:
            embed = fmt.info("🎙 Выход из голосового", f"{member.mention} ← {before.channel.mention}", fmt.RED)
        else:
            embed = fmt.info("🎙 Перемещение", f"{member.mention}: {before.channel.mention} → {after.channel.mention}",
                             fmt.YELLOW)
        self._send("voice", embed)

    # ── команды

    @logs_group.command(name="test", description="Отправить тестовое сообщение в #логи")
    @min_level(5, "Это могут Совет и выше.")
    async def test(self, interaction: discord.Interaction) -> None:
        self.service.send("bot", fmt.info("🧪 Тест логов", f"Проверка от {interaction.user.mention}."), force=True)
        await interaction.response.send_message("Тестовое сообщение поставлено в очередь.", ephemeral=True)

    @logs_group.command(name="toggle", description="Включить или выключить категорию логов")
    @app_commands.choices(category=CATEGORY_CHOICES)
    @min_level(5, "Это могут Совет и выше.")
    async def toggle(self, interaction: discord.Interaction, category: app_commands.Choice[str],
                     enabled: bool) -> None:
        await self.service.set_enabled(category.value, enabled)
        state = "включена" if enabled else "выключена"
        self.service.bot_event("⚙️ Логи", f"{interaction.user.mention}: категория «{CATEGORY_TITLES[category.value]}» "
                                          f"{state}.", color="yellow")
        await interaction.response.send_message(f"Категория «{CATEGORY_TITLES[category.value]}» {state}.",
                                                ephemeral=True)

    @logs_group.command(name="status", description="Какие категории логов включены")
    @min_level(5, "Это могут Совет и выше.")
    async def status(self, interaction: discord.Interaction) -> None:
        lines = [f"{'✅' if self.service.is_enabled(k) else '⛔'} {CATEGORY_TITLES[k]} (`{k}`)"
                 for k in config.LOG_CATEGORIES]
        lines.append(f"\nВ очереди: {self.service.queue.qsize()} · отправлено: {self.service.sent} · "
                     f"потеряно: {self.service.dropped}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(Logs(bot))
