"""Верификация: автороль при входе, анкета, решение Доверенных."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .. import config
from ..content import texts
from ..embeds import make_embed
from ..logic.verification import (
    ACCEPTED,
    PENDING,
    REJECTED,
    SUBMITTED,
    AlreadyDecided,
    Answers,
    Attempt,
    check_submission,
    human_duration,
    next_status,
    validate_answers,
)
from ..setup_runner import register_panel
from ..ui import Denied, SafeModal, SafeView, member_level, report_error, send_ephemeral

if TYPE_CHECKING:
    from ..bot import VeritasBot
    from ..db import Database

log = logging.getLogger("veritas.verification")

VERIFY_BUTTON_ID = "veritas:verify:start"
DECIDE_LEVEL = 4  # Доверенный и выше


# ───────────────────── ХРАНИЛИЩЕ ─────────────────────


class VerificationStore:
    def __init__(self, db: Database):
        self.db = db

    async def attempts(self, user_id: int, since: float) -> list[Attempt]:
        rows = await self.db.fetchall(
            "SELECT ts, result FROM verification_attempts WHERE user_id = ? AND ts >= ? ORDER BY ts",
            (user_id, since))
        return [Attempt(r["ts"], r["result"]) for r in rows]

    async def record_attempt(self, user_id: int, ts: float, result: str) -> None:
        await self.db.execute("INSERT INTO verification_attempts (user_id, ts, result) VALUES (?, ?, ?)",
                              (user_id, ts, result))

    async def pending_for(self, user_id: int):
        return await self.db.fetchone(
            "SELECT * FROM applications WHERE user_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (user_id, PENDING))

    async def create(self, user_id: int, answers: Answers, flags: list[str], now: float) -> int:
        return await self.db.execute(
            "INSERT INTO applications (user_id, answers_json, status, flags, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, json.dumps(answers.as_dict(), ensure_ascii=False), PENDING, "\n".join(flags), now))

    async def set_message(self, app_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute("UPDATE applications SET channel_id = ?, message_id = ? WHERE id = ?",
                              (channel_id, message_id, app_id))

    async def get(self, app_id: int):
        return await self.db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))

    async def decide(self, app_id: int, action: str, moderator_id: int, reason: str | None,
                     now: float) -> str:
        """Атомарно перевести анкету из pending. Повторное решение → AlreadyDecided."""
        row = await self.get(app_id)
        if row is None:
            raise LookupError(app_id)
        status = next_status(row["status"], action)
        changed = await self.db.execute(
            "UPDATE applications SET status = ?, decided_by = ?, decided_at = ?, reason = ? "
            "WHERE id = ? AND status = ?",
            (status, moderator_id, now, reason, app_id, PENDING))
        if changed == 0:
            fresh = await self.get(app_id)
            raise AlreadyDecided(fresh["status"])
        return status

    async def revert(self, app_id: int) -> None:
        await self.db.execute(
            "UPDATE applications SET status = ?, decided_by = NULL, decided_at = NULL, reason = NULL "
            "WHERE id = ?", (PENDING, app_id))

    async def was_accepted(self, user_id: int) -> bool:
        return await self.db.fetchone(
            "SELECT 1 FROM applications WHERE user_id = ? AND status = ?", (user_id, ACCEPTED)) is not None

    async def pending_count(self) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM applications WHERE status = ?", (PENDING,), 0)


# ───────────────────── EMBED-Ы И КНОПКИ ─────────────────────

STATUS_TEXT = {PENDING: "🕯 Ожидает решения", ACCEPTED: "✅ Принята", REJECTED: "❌ Отклонена"}


def application_embed(app, *, user_mention: str, account_created: datetime | None,
                      decided_by_mention: str | None = None, auto: bool = False) -> discord.Embed:
    answers = json.loads(app["answers_json"])
    status = app["status"]
    color = {PENDING: "blue", ACCEPTED: "green", REJECTED: "red"}[status]
    fields = [
        ("Кандидат", f"{user_mention} (`{app['user_id']}`)", True),
        ("Аккаунт создан",
         f"<t:{int(account_created.timestamp())}:R>" if account_created else "неизвестно", True),
        (texts.Q_NAME, answers.get("name", ""), False),
        (texts.Q_SOURCE, answers.get("source", ""), False),
        (texts.Q_WHY, answers.get("why", ""), False),
        (texts.Q_AGREE, answers.get("agree", ""), True),
    ]
    if app["flags"]:
        fields.append(("Пометки", app["flags"], False))
    if status != PENDING:
        who = "бот (авто-режим)" if auto else (decided_by_mention or f"<@{app['decided_by']}>")
        when = f"<t:{int(app['decided_at'])}:f>" if app["decided_at"] else ""
        line = f"{STATUS_TEXT[status]} — {who} {when}".strip()
        if status == REJECTED:
            line += f"\nПричина: {app['reason'] or texts.NO_REASON}"
        fields.append(("Решение", line, False))
    return make_embed(f"📋 Анкета #{app['id']}", None, color=color, fields=fields,
                      footer=STATUS_TEXT[status])


class AppDecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"veritas:app:(?P<action>accept|reject):(?P<id>\d+)"):
    """Кнопки «Принять»/«Отклонить» под анкетой; переживают перезапуск бота."""

    def __init__(self, action: str, app_id: int):
        label, style = (("✅ Принять", discord.ButtonStyle.success) if action == "accept"
                        else ("❌ Отклонить", discord.ButtonStyle.danger))
        super().__init__(discord.ui.Button(label=label, style=style,
                                           custom_id=f"veritas:app:{action}:{app_id}"))
        self.action = action
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if member_level(interaction) < DECIDE_LEVEL:
            await send_ephemeral(interaction, "Анкеты решают Доверенные и выше.")
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Verification = interaction.client.get_cog("Verification")
        try:
            if self.action == "accept":
                await cog.on_accept_button(interaction, self.app_id)
            else:
                await cog.on_reject_button(interaction, self.app_id)
        except Exception as exc:  # noqa: BLE001 — ответить человеку и не уронить бота
            await report_error(interaction, exc, f"анкета #{self.app_id}")


def decision_view(app_id: int) -> discord.ui.View:
    view = SafeView(timeout=None)
    view.add_item(AppDecisionButton("accept", app_id))
    view.add_item(AppDecisionButton("reject", app_id))
    return view


def closed_view(status: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=STATUS_TEXT[status], disabled=True,
                                    style=discord.ButtonStyle.secondary,
                                    custom_id=f"veritas:app:closed:{status}"))
    return view


class VerifyPanelView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=texts.VERIFY_BUTTON, emoji="🕯", style=discord.ButtonStyle.primary,
                       custom_id=VERIFY_BUTTON_ID)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("Verification").start(interaction)


def verify_panel(settings) -> dict:
    return {"content": None,
            "embeds": [make_embed(texts.VERIFY_PANEL_TITLE, texts.VERIFY_PANEL, color="brand")],
            "view": VerifyPanelView()}


register_panel("verify_panel", "verify", verify_panel)


class VerifyModal(SafeModal, title=texts.VERIFY_MODAL_TITLE):
    name = discord.ui.TextInput(label=texts.Q_NAME, max_length=64, min_length=2)
    source = discord.ui.TextInput(label=texts.Q_SOURCE, max_length=200, min_length=2)
    why = discord.ui.TextInput(label=texts.Q_WHY, style=discord.TextStyle.paragraph,
                               min_length=config.VERIFICATION.min_reason_len, max_length=1000)
    agree = discord.ui.TextInput(label=texts.Q_AGREE, max_length=20)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = Answers(self.name.value, self.source.value, self.why.value, self.agree.value)
        await interaction.client.get_cog("Verification").submit(interaction, answers)


class RejectModal(SafeModal, title="Отклонить анкету"):
    reason = discord.ui.TextInput(label="Причина (необязательно)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=500)

    def __init__(self, app_id: int):
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: Verification = interaction.client.get_cog("Verification")
        await cog.on_reject_submit(interaction, self.app_id, self.reason.value.strip() or None)


# ───────────────────── МОДУЛЬ ─────────────────────


@dataclass
class DecideResult:
    ok: bool
    message: str
    app: object | None = None


class Verification(commands.Cog):
    def __init__(self, bot: VeritasBot):
        self.bot = bot
        self.store = VerificationStore(bot.db)
        self.cfg = config.VERIFICATION

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyPanelView())
        self.bot.add_dynamic_items(AppDecisionButton)

    # ── вход на сервер

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.bot.is_home(member.guild):
            return
        gm = self.bot.guild_map(member.guild)
        if gm is None:
            return
        if await self.bot.db.is_exiled(member.id):
            role = gm.role(config.EXILE)
            if role:
                await member.add_roles(role, reason="Veritas: метка Отлучённого сохраняется")
            self.bot.log_event("🚫 Отлучённый вернулся",
                               f"{member.mention} снова на сервере — выдана метка Отлучённого.", color="red")
            return
        keys = [config.STRANGER]
        if self.bot.settings.restore_on_rejoin and await self.store.was_accepted(member.id):
            keys = [config.FOLL]
        if await self.bot.db.is_liar(member.id):
            keys.append(config.LIAR)
        roles = gm.roles(keys)
        if roles:
            await member.add_roles(*roles, reason="Veritas: роль при входе")

    # ── панель

    async def start(self, interaction: discord.Interaction) -> None:
        keys = self._keys(interaction)
        if config.EXILE in keys:
            raise Denied(texts.VERIFY_EXILED)
        if member_level(interaction) >= 2:
            raise Denied(texts.VERIFY_ALREADY)
        decision = await self._decision(interaction.user, time.time())
        if not decision.allowed:
            raise Denied(decision.reason)
        await interaction.response.send_modal(VerifyModal())

    def _keys(self, interaction: discord.Interaction) -> set[str]:
        gm = self.bot.guild_map(interaction.guild)
        return gm.member_keys(interaction.user) if gm else set()

    async def _decision(self, user: discord.abc.User, now: float):
        attempts = await self.store.attempts(user.id, now - 3 * 24 * 3600)
        pending = await self.store.pending_for(user.id)
        created = getattr(user, "created_at", None)
        age = (now - created.timestamp()) / 86400 if created else None
        return check_submission(now, attempts, has_pending=pending is not None, account_age_days=age,
                                cfg=self.cfg)

    # ── отправка анкеты

    async def submit(self, interaction: discord.Interaction, answers: Answers) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.user
        now = time.time()
        decision = await self._decision(member, now)
        if not decision.allowed:
            raise Denied(decision.reason)
        errors = validate_answers(answers, self.cfg)
        if errors:
            raise Denied(texts.VERIFY_INVALID.format(errors="\n".join(f"• {e}" for e in errors)))
        app_id = await self.store.create(member.id, answers, decision.flags, now)
        await self.store.record_attempt(member.id, now, SUBMITTED)
        gm = self.bot.guild_map(interaction.guild)
        channel = gm.channel("applications") if gm else None
        auto = self.bot.settings.verify_mode == "auto" and not decision.manual

        if auto:
            result = await self.decide(interaction.guild, app_id, "accept", self.bot.user, None)
            if result.ok:
                if channel is not None:
                    await channel.send(embed=application_embed(
                        result.app, user_mention=member.mention, account_created=member.created_at, auto=True))
                await send_ephemeral(interaction, texts.ACCEPTED_DM)
                return
            # не удалось выдать роль — анкета остаётся на ручную проверку
        if channel is None:
            self.bot.log_event("⚠️ Анкета без канала", f"Анкета #{app_id} от {member.mention}: "
                               "канал #анкеты не найден. Запусти /setup run.", color="yellow")
            await send_ephemeral(interaction, texts.VERIFY_SUBMITTED_NO_CHANNEL)
            return
        app = await self.store.get(app_id)
        msg = await channel.send(embed=application_embed(app, user_mention=member.mention,
                                                         account_created=member.created_at),
                                 view=decision_view(app_id))
        await self.store.set_message(app_id, channel.id, msg.id)
        await send_ephemeral(interaction, texts.VERIFY_SUBMITTED)

    # ── решение (ядро, без interaction — тестируется на подделках)

    async def decide(self, guild: discord.Guild, app_id: int, action: str, moderator: discord.abc.User,
                     reason: str | None) -> DecideResult:
        app = await self.store.get(app_id)
        if app is None:
            return DecideResult(False, "Анкета не найдена.")
        if app["status"] != PENDING:
            return DecideResult(False, f"Анкета уже решена: {STATUS_TEXT[app['status']]}.", app)
        member = guild.get_member(app["user_id"])
        if member is None and action == "accept":
            try:
                member = await guild.fetch_member(app["user_id"])
            except discord.NotFound:
                member = None
        if member is None and action == "accept":
            return DecideResult(False, "Человек уже покинул сервер — принять анкету нельзя.", app)
        now = time.time()
        try:
            await self.store.decide(app_id, action, moderator.id, reason, now)
        except AlreadyDecided as exc:
            return DecideResult(False, f"Анкета уже решена: {STATUS_TEXT.get(exc.status, exc.status)}.", app)

        if action == "accept":
            try:
                await self._grant_follower(guild, member)
            except Exception:
                await self.store.revert(app_id)
                raise
            await self.store.record_attempt(member.id, now, ACCEPTED)
            await _dm(member, texts.ACCEPTED_DM)
            self.bot.log_event("✅ Анкета принята",
                               f"Анкета #{app_id}: {member.mention} принят — {moderator.mention}.", color="green")
            return DecideResult(True, f"Принято: {member.mention} теперь Последователь.", await self.store.get(app_id))

        await self.store.record_attempt(app["user_id"], now, REJECTED)
        if member is not None:
            await _dm(member, texts.REJECTED_DM.format(reason=reason or texts.NO_REASON,
                                                       cooldown=human_duration(self.cfg.reject_cooldown_s)))
        self.bot.log_event("❌ Анкета отклонена",
                           f"Анкета #{app_id} (<@{app['user_id']}>) отклонена — {moderator.mention}. "
                           f"Причина: {reason or texts.NO_REASON}", color="red")
        return DecideResult(True, "Анкета отклонена.", await self.store.get(app_id))

    async def _grant_follower(self, guild: discord.Guild, member: discord.Member) -> None:
        gm = self.bot.guild_map(guild)
        foll = gm.role(config.FOLL) if gm else None
        if foll is None:
            raise Denied("Роль Последователя не найдена — запусти /setup run.")
        await member.add_roles(foll, reason="Veritas: анкета принята")
        stranger = gm.role(config.STRANGER)
        if stranger is not None and stranger in member.roles:
            await member.remove_roles(stranger, reason="Veritas: анкета принята")

    # ── кнопки

    async def on_accept_button(self, interaction: discord.Interaction, app_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.decide(interaction.guild, app_id, "accept", interaction.user, None)
        await self._finish(interaction, result)

    async def on_reject_button(self, interaction: discord.Interaction, app_id: int) -> None:
        app = await self.store.get(app_id)
        if app is None or app["status"] != PENDING:
            status = STATUS_TEXT.get(app["status"], "?") if app else "не найдена"
            raise Denied(f"Анкета уже решена: {status}.")
        await interaction.response.send_modal(RejectModal(app_id))

    async def on_reject_submit(self, interaction: discord.Interaction, app_id: int, reason: str | None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.decide(interaction.guild, app_id, "reject", interaction.user, reason)
        await self._finish(interaction, result)

    async def _finish(self, interaction: discord.Interaction, result: DecideResult) -> None:
        await send_ephemeral(interaction, result.message)
        app = result.app
        if app is None or app["status"] == PENDING or not app["message_id"]:
            return
        channel = interaction.guild.get_channel(app["channel_id"])
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(app["message_id"])
            member = interaction.guild.get_member(app["user_id"])
            await msg.edit(embed=application_embed(
                app, user_mention=f"<@{app['user_id']}>",
                account_created=member.created_at if member else _snowflake_time(app["user_id"]),
                decided_by_mention=f"<@{app['decided_by']}>"), view=closed_view(app["status"]))
        except discord.HTTPException:
            log.warning("Не удалось обновить сообщение анкеты #%s", app["id"])


def _snowflake_time(user_id: int) -> datetime:
    return discord.utils.snowflake_time(user_id).astimezone(UTC)


async def _dm(member: discord.abc.User, text: str) -> None:
    try:
        await member.send(text)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass  # ЛС закрыты — ничего страшного


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(Verification(bot))
