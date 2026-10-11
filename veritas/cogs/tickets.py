"""Тикеты: приватные каналы для обращений, транскрипты, автозакрытие."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config
from ..content import texts
from ..embeds import make_embed
from ..logic.tickets import (
    CLOSED,
    OPEN,
    TICKET_TYPES,
    TranscriptMessage,
    build_transcript,
    channel_name,
    check_limits,
    handler_roles,
    inactivity_action,
    is_handler,
    slugify,
)
from ..permissions import ticket_overwrites
from ..setup_runner import register_panel, to_discord_overwrites, upsert_message
from ..ui import Denied, SafeModal, SafeView, top_only

if TYPE_CHECKING:
    from ..bot import VeritasBot
    from ..db import Database

log = logging.getLogger("veritas.tickets")


# ───────────────────── ХРАНИЛИЩЕ ─────────────────────


class TicketStore:
    def __init__(self, db: Database):
        self.db = db
        self.lock = asyncio.Lock()

    async def open_types(self, user_id: int) -> list[str]:
        rows = await self.db.fetchall("SELECT type FROM tickets WHERE user_id = ? AND status = ?",
                                      (user_id, OPEN))
        return [r["type"] for r in rows]

    async def last_created(self, user_id: int) -> float | None:
        return await self.db.fetchval("SELECT MAX(created_at) FROM tickets WHERE user_id = ?", (user_id,))

    async def reserve(self, user_id: int, ticket_type: str, now: float) -> tuple[int, int]:
        """Занять следующий номер. Возвращает (id, номер)."""
        number = await self.db.fetchval("SELECT MAX(number) FROM tickets", (), 0) + 1
        tid = await self.db.execute(
            "INSERT INTO tickets (number, user_id, type, status, created_at, last_activity) "
            "VALUES (?, ?, ?, ?, ?, ?)", (number, user_id, ticket_type, OPEN, now, now))
        return tid, number

    async def drop(self, ticket_id: int) -> None:
        """Убрать запись тикета, канал которого так и не был создан."""
        await self.db.execute("DELETE FROM tickets WHERE id = ? AND channel_id IS NULL", (ticket_id,))

    async def set_channel(self, ticket_id: int, channel_id: int) -> None:
        await self.db.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel_id, ticket_id))

    async def by_channel(self, channel_id: int):
        return await self.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))

    async def claim(self, ticket_id: int, user_id: int) -> None:
        await self.db.execute("UPDATE tickets SET claimed_by = ? WHERE id = ?", (user_id, ticket_id))

    async def begin_close(self, ticket_id: int, closer_id: int, reason: str, now: float) -> bool:
        changed = await self.db.execute(
            "UPDATE tickets SET status = ?, closed_at = ?, closed_by = ?, close_reason = ? "
            "WHERE id = ? AND status = ?", (CLOSED, now, closer_id, reason, ticket_id, OPEN))
        return changed == 1

    async def touch(self, channel_id: int, now: float) -> None:
        await self.db.execute(
            "UPDATE tickets SET last_activity = ?, inactivity_warned = 0 WHERE channel_id = ? AND status = ?",
            (now, channel_id, OPEN))

    async def set_warned(self, ticket_id: int) -> None:
        await self.db.execute("UPDATE tickets SET inactivity_warned = 1 WHERE id = ?", (ticket_id,))

    async def open_tickets(self):
        return await self.db.fetchall("SELECT * FROM tickets WHERE status = ? AND channel_id IS NOT NULL",
                                      (OPEN,))

    async def open_channel_ids(self) -> set[int]:
        return {r["channel_id"] for r in await self.open_tickets()}

    async def open_count(self) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM tickets WHERE status = ?", (OPEN,), 0)


# ───────────────────── ИНТЕРФЕЙС ─────────────────────


class TicketPanelView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)
        for t in TICKET_TYPES.values():
            if not t.on_panel:
                continue
            button = discord.ui.Button(label=t.label, emoji=t.emoji, style=discord.ButtonStyle.secondary,
                                       custom_id=f"veritas:ticket:open:{t.key}")
            button.callback = self._make_callback(t.key)
            self.add_item(button)

    @staticmethod
    def _make_callback(ticket_type: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.client.get_cog("Tickets").open_ticket(interaction, ticket_type)
        return callback


def tickets_panel(settings) -> dict:
    return {"content": None,
            "embeds": [make_embed(texts.TICKETS_PANEL_TITLE, texts.TICKETS_PANEL)],
            "view": TicketPanelView()}


register_panel("tickets_panel", "requests", tickets_panel)


# Ключ отслеживаемого сообщения: тот же, что у старого шаблона приёма, — /mainez в
# #приём-материалов превратит прежний закреп в панель, а не добавит второй.
MATERIALS_KEY = "intake"


class MaterialsPanelView(SafeView):
    """Кнопка в #приём-материалов: открывает тикет «Отправка материала»."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Отправить материал", emoji="📥", style=discord.ButtonStyle.success,
                       custom_id="veritas:ticket:open:materials")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("Tickets").open_ticket(interaction, "materials")


def materials_panel(settings) -> dict:
    return {"content": None,
            "embeds": [make_embed(texts.MATERIALS_PANEL_TITLE, texts.MATERIALS_PANEL, color="gold")],
            "view": MaterialsPanelView()}


class TicketControlView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Взять", emoji="🙋", style=discord.ButtonStyle.primary,
                       custom_id="veritas:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("Tickets").claim(interaction)

    @discord.ui.button(label="Закрыть", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="veritas:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: Tickets = interaction.client.get_cog("Tickets")
        await cog.require_closer(interaction)
        await interaction.response.send_modal(CloseModal())


class CloseModal(SafeModal, title="Закрыть тикет"):
    reason = discord.ui.TextInput(label="Причина (необязательно)", required=False, max_length=300,
                                  style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog: Tickets = interaction.client.get_cog("Tickets")
        msg = await cog.close_ticket(interaction.guild, interaction.channel, interaction.user,
                                     self.reason.value.strip() or None)
        await interaction.followup.send(msg, ephemeral=True)


def ticket_embed(ticket, author_mention: str, handlers: str, claimed_by: int | None = None) -> discord.Embed:
    t = TICKET_TYPES[ticket["type"]]
    fields = [("Тип", f"{t.emoji} {t.label}", True), ("Автор", author_mention, True),
              ("Обработчики", handlers, False), ("Что делать", t.hint, False)]
    if claimed_by:
        fields.append(("Взял", f"<@{claimed_by}>", True))
    return make_embed(f"🎫 Тикет №{ticket['number']:04d}", None, color="blue", fields=fields)


# ───────────────────── МОДУЛЬ ─────────────────────


class Tickets(commands.Cog):
    ticket_group = app_commands.Group(name="ticket", description="Управление тикетом", guild_only=True)

    def __init__(self, bot: VeritasBot):
        self.bot = bot
        self.store = TicketStore(bot.db)
        self.cfg = config.TICKETS
        self._closing: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(MaterialsPanelView())
        self.bot.add_view(TicketControlView())
        self.inactivity_loop.start()

    async def cog_unload(self) -> None:
        self.inactivity_loop.cancel()

    # ── помощники

    def _keys(self, guild, user) -> set[str]:
        gm = self.bot.guild_map(guild)
        return gm.member_keys(user) if gm else set()

    def is_handler(self, guild, user, ticket_type: str) -> bool:
        return is_handler(self._keys(guild, user), ticket_type)

    def handler_mentions(self, guild, gm, ticket_type: str) -> str:
        keys = handler_roles(ticket_type)
        parts = [r.mention for r in gm.roles(keys)] if gm else []
        if config.FOUNDER in keys and (gm is None or gm.role(config.FOUNDER) is None) and guild.owner_id:
            parts.append(f"<@{guild.owner_id}>")  # роли Основателя нет — тегаем владельца сервера
        return ", ".join(parts) or "Совет"

    async def _ticket_here(self, channel) -> object:
        ticket = await self.store.by_channel(getattr(channel, "id", 0))
        if ticket is None or ticket["status"] != OPEN:
            raise Denied("Эта команда работает только в открытом тикете.")
        return ticket

    async def require_closer(self, interaction: discord.Interaction):
        ticket = await self._ticket_here(interaction.channel)
        if interaction.user.id != ticket["user_id"] and not self.is_handler(
                interaction.guild, interaction.user, ticket["type"]):
            raise Denied("Закрыть тикет могут автор и обработчики.")
        return ticket

    async def require_handler(self, interaction: discord.Interaction):
        ticket = await self._ticket_here(interaction.channel)
        if not self.is_handler(interaction.guild, interaction.user, ticket["type"]):
            raise Denied("Это могут только обработчики тикета.")
        return ticket

    # ── открытие

    async def open_ticket(self, interaction: discord.Interaction, ticket_type: str) -> None:
        guild, member = interaction.guild, interaction.user
        keys = self._keys(guild, member)
        if config.EXILE in keys:
            raise Denied(texts.TICKET_EXILED)
        await interaction.response.defer(ephemeral=True, thinking=True)
        gm = self.bot.guild_map(guild)
        category = gm.channel("cat_tickets") if gm else None
        if category is None:
            raise Denied("Категория тикетов не найдена. Совету нужно запустить /setup run.")
        async with self.store.lock:
            now = time.time()
            ok, reason = check_limits(await self.store.open_types(member.id), ticket_type,
                                      await self.store.last_created(member.id), now, self.cfg)
            if not ok:
                raise Denied(reason)
            ticket_id, number = await self.store.reserve(member.id, ticket_type, now)
        handlers = handler_roles(ticket_type)
        overwrites = to_discord_overwrites(ticket_overwrites(handlers), guild, gm, extra={"@author": member})
        try:
            channel = await guild.create_text_channel(
                channel_name(number, member.display_name, TICKET_TYPES[ticket_type].prefix),
                category=category, overwrites=overwrites,
                topic=f"Тикет №{number:04d} · {TICKET_TYPES[ticket_type].label} · автор {member.id}",
                reason=f"Veritas: тикет №{number}")
        except Exception:
            await self.store.drop(ticket_id)
            raise
        await self.store.set_channel(ticket_id, channel.id)
        ticket = await self.store.by_channel(channel.id)
        handler_text = self.handler_mentions(guild, gm, ticket_type)
        t = TICKET_TYPES[ticket_type]
        if t.ping_handlers:
            intro = texts.MATERIALS_WELCOME.format(handlers=handler_text, author=member.mention)
        else:
            intro = texts.TICKET_WELCOME.format(author=member.mention, handlers=handler_text)
        await channel.send(intro, embed=ticket_embed(ticket, member.mention, handler_text),
                           view=TicketControlView(),
                           allowed_mentions=discord.AllowedMentions(users=True, roles=t.ping_handlers))
        if ticket_type == "materials":
            await channel.send(texts.MATERIALS_REQUEST.format(author=member.mention),
                               allowed_mentions=discord.AllowedMentions(users=True))
        self.bot.log_event("🎫 Тикет открыт",
                           f"№{number:04d} · {TICKET_TYPES[ticket_type].label} · {member.mention} · {channel.mention}",
                           color="green")
        await interaction.followup.send(texts.TICKET_OPENED.format(channel=channel.mention), ephemeral=True)

    # ── взять

    async def claim(self, interaction: discord.Interaction) -> None:
        ticket = await self.require_handler(interaction)
        if ticket["claimed_by"] == interaction.user.id:
            raise Denied("Ты уже ведёшь этот тикет.")
        await self.store.claim(ticket["id"], interaction.user.id)
        gm = self.bot.guild_map(interaction.guild)
        handler_text = self.handler_mentions(interaction.guild, gm, ticket["type"])
        try:
            await interaction.message.edit(embed=ticket_embed(ticket, f"<@{ticket['user_id']}>", handler_text,
                                                              claimed_by=interaction.user.id))
        except (discord.HTTPException, AttributeError):
            pass
        prev = f" (раньше вёл <@{ticket['claimed_by']}>)" if ticket["claimed_by"] else ""
        await interaction.response.send_message(f"🙋 {interaction.user.mention} взял тикет{prev}.",
                                                allowed_mentions=discord.AllowedMentions.none())

    # ── закрытие

    async def close_ticket(self, guild: discord.Guild, channel, closer: discord.abc.User,
                           reason: str | None, *, delete_delay: float | None = None) -> str:
        ticket = await self.store.by_channel(getattr(channel, "id", 0))
        if ticket is None:
            return "Это не тикет."
        reason = reason or "не указана"
        if ticket["id"] in self._closing or not await self.store.begin_close(
                ticket["id"], closer.id, reason, time.time()):
            return "Тикет уже закрывается."
        self._closing.add(ticket["id"])
        try:
            delay = self.cfg.delete_delay_s if delete_delay is None else delete_delay
            try:
                await channel.send(texts.TICKET_CLOSING.format(closer=closer.mention, reason=reason, delay=int(delay)),
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            transcript = await self.transcript(guild, channel, ticket, closer, reason)
            await self._deliver_transcript(guild, ticket, transcript, closer, reason)
        except Exception:
            self._closing.discard(ticket["id"])
            raise
        self.bot.log_event("🔒 Тикет закрыт", f"№{ticket['number']:04d} · закрыл {closer.mention} · {reason}",
                           color="red")
        task = asyncio.create_task(self._delete_later(guild, channel, ticket["id"], delay))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"Тикет №{ticket['number']:04d} закрыт."

    async def _delete_later(self, guild, channel, ticket_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._delete_ticket_channel(guild, channel)
        except Exception:
            log.exception("Не удалось удалить канал тикета")
        finally:
            self._closing.discard(ticket_id)

    async def transcript(self, guild, channel, ticket, closer, reason) -> str:
        messages = []
        try:
            async for m in channel.history(limit=self.cfg.transcript_limit, oldest_first=True):
                messages.append(TranscriptMessage(
                    ts=m.created_at, author=str(m.author), author_id=m.author.id, content=m.content or "",
                    attachments=[a.url for a in m.attachments], embeds=len(m.embeds)))
        except discord.HTTPException:
            log.warning("Не удалось прочитать историю тикета №%s", ticket["number"])
        header = {
            "Тикет": f"№{ticket['number']:04d}",
            "Тип": TICKET_TYPES[ticket["type"]].label,
            "Автор": f"{ticket['user_id']}",
            "Открыт": _fmt_ts(ticket["created_at"]),
            "Закрыл": f"{closer} ({closer.id})",
            "Причина": reason,
        }
        return build_transcript(header, messages)

    async def _deliver_transcript(self, guild, ticket, transcript: str, closer, reason: str) -> None:
        name = f"ticket-{ticket['number']:04d}.txt"
        gm = self.bot.guild_map(guild)
        logs = gm.channel("logs") if gm else None
        if logs is not None:
            embed = make_embed("🔒 Тикет закрыт", None, color="red", fields=[
                ("Тикет", f"№{ticket['number']:04d} · {TICKET_TYPES[ticket['type']].label}", True),
                ("Автор", f"<@{ticket['user_id']}>", True),
                ("Закрыл", closer.mention, True),
                ("Причина", reason, False)])
            try:
                await logs.send(embed=embed, file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=name))
            except discord.HTTPException:
                log.warning("Не удалось отправить транскрипт в #логи")
        author = guild.get_member(ticket["user_id"])
        if author is not None:
            try:
                await author.send(texts.TICKET_DM_TRANSCRIPT.format(number=f"{ticket['number']:04d}", reason=reason),
                                  file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=name))
            except (discord.HTTPException, AttributeError):
                pass  # ЛС закрыты

    async def _delete_ticket_channel(self, guild, channel) -> None:
        """Единственное удаление каналов ботом: только канал тикета из БД в категории тикетов."""
        ticket = await self.store.by_channel(channel.id)
        gm = self.bot.guild_map(guild)
        category = gm.channel("cat_tickets") if gm else None
        if ticket is None or category is None or channel.category_id != category.id:
            log.warning("Канал %s не похож на тикет — не удаляю", channel.id)
            return
        try:
            await channel.delete(reason=f"Veritas: тикет №{ticket['number']} закрыт")
        except discord.NotFound:
            pass

    # ── активность и автозакрытие

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.bot.is_home(message.guild):
            return
        category_id = getattr(message.channel, "category_id", None)
        if category_id and category_id == self.bot.channel_ids.get("cat_tickets"):
            await self.store.touch(message.channel.id, time.time())

    @tasks.loop(minutes=30)
    async def inactivity_loop(self) -> None:
        guild = self.bot.home_guild
        if guild is None:
            return
        now = time.time()
        for ticket in await self.store.open_tickets():
            action = inactivity_action(ticket["last_activity"], bool(ticket["inactivity_warned"]), now, self.cfg)
            channel = guild.get_channel(ticket["channel_id"])
            if action is None or channel is None:
                continue
            try:
                if action == "warn":
                    days = self.cfg.inactivity_close_s // 86400
                    await channel.send(texts.TICKET_INACTIVE_WARN.format(days=days))
                    await self.store.set_warned(ticket["id"])
                else:
                    await self.close_ticket(guild, channel, guild.me, "неактивность")
            except discord.HTTPException:
                log.exception("Автозакрытие тикета №%s", ticket["number"])

    @inactivity_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── команды

    @app_commands.command(name="mainez", description="Опубликовать панель приёма материалов в этом канале")
    @app_commands.guild_only()
    @top_only()
    async def mainez(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            raise Denied("Панель можно опубликовать только в текстовом канале.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        line = await upsert_message(self.bot.db, interaction.guild, MATERIALS_KEY, interaction.channel,
                                    materials_panel(self.bot.settings), dry_run=False)
        self.bot.log_event("📥 Панель материалов", f"{interaction.user.mention} опубликовал панель в "
                                                  f"{interaction.channel.mention}.")
        await interaction.followup.send(f"Готово.{line.replace('    ', ' ')}", ephemeral=True)

    @ticket_group.command(name="add", description="Добавить участника в тикет")
    async def add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ticket = await self.require_handler(interaction)
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True,
                                                  read_message_history=True, attach_files=True,
                                                  reason=f"Veritas: добавлен в тикет №{ticket['number']}")
        await interaction.response.send_message(f"➕ {user.mention} добавлен в тикет.")

    @ticket_group.command(name="remove", description="Убрать участника из тикета")
    async def remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ticket = await self.require_handler(interaction)
        if user.id == ticket["user_id"]:
            raise Denied("Автора тикета убрать нельзя — закрой тикет.")
        await interaction.channel.set_permissions(user, overwrite=None,
                                                  reason=f"Veritas: убран из тикета №{ticket['number']}")
        await interaction.response.send_message(f"➖ {user.mention} убран из тикета.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @ticket_group.command(name="close", description="Закрыть тикет")
    @app_commands.describe(reason="Причина (необязательно)")
    async def close(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self.require_closer(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.close_ticket(interaction.guild, interaction.channel, interaction.user, reason)
        await interaction.followup.send(msg, ephemeral=True)

    @ticket_group.command(name="rename", description="Переименовать тикет")
    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        ticket = await self.require_handler(interaction)
        new = f"{TICKET_TYPES[ticket['type']].prefix}-{ticket['number']:04d}-{slugify(name, 40)}"
        await interaction.channel.edit(name=new, reason="Veritas: переименование тикета")
        await interaction.response.send_message(f"✏️ Тикет переименован: `{new}`", ephemeral=True)


def _fmt_ts(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(Tickets(bot))
