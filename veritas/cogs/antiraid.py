"""Защита: anti-raid (локдаун), фильтры входа, anti-spam, anti-nuke, защита структуры."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config
from ..config import ALL, override_dataclass
from ..embeds import make_embed
from ..logic.raid import (
    DESTRUCTIVE_LABELS,
    NukeDetector,
    OffenseCounter,
    RaidDetector,
    SpamTracker,
    classify_audit,
    ladder_action,
    link_violation,
    suspicion,
)
from ..ui import Denied, SafeView, member_level, min_level, report_error, send_ephemeral

if TYPE_CHECKING:
    from ..bot import VeritasBot

log = logging.getLogger("veritas.antiraid")

LOCK_STATE_KEY = "lockdown"
CFG_SECTIONS = ("raid", "spam", "nuke")


def closed_keys() -> set[str]:
    """Категории и каналы, которые @everyone видеть не должен."""
    keys = set()
    for cat in config.STRUCTURE:
        if cat.view is not ALL:
            keys.add(cat.key)
        keys |= {c.key for c in cat.channels if c.view is not ALL}
    return keys


# ───────────────────── КНОПКИ ─────────────────────


class LockdownView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if member_level(interaction) < 5:
            await send_ephemeral(interaction, "Локдауном управляет Совет.")
            return False
        return True

    @discord.ui.button(label="Снять локдаун", emoji="🔓", style=discord.ButtonStyle.success,
                       custom_id="veritas:raid:lift")
    async def lift(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: AntiRaid = interaction.client.get_cog("AntiRaid")
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await cog.lift_lockdown(interaction.guild, by=interaction.user)
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Кикнуть вошедших во время рейда", emoji="👢", style=discord.ButtonStyle.danger,
                       custom_id="veritas:raid:kick")
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: AntiRaid = interaction.client.get_cog("AntiRaid")
        raiders = cog.state.get("raiders", [])
        if not raiders:
            raise Denied("Список вошедших во время рейда пуст.")
        await interaction.response.send_message(
            f"Кикнуть {len(raiders)} человек, вошедших во время рейда? Это действие нельзя отменить.",
            view=ConfirmKickView(), ephemeral=True)


class ConfirmKickView(SafeView):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Да, кикнуть", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if member_level(interaction) < 5:
            raise Denied("Это может только Совет.")
        await interaction.response.edit_message(content="Кикаю…", view=None)
        cog: AntiRaid = interaction.client.get_cog("AntiRaid")
        count = await cog.kick_raiders(interaction.guild, interaction.user)
        await interaction.followup.send(f"Кикнуто: {count}.", ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Отменено.", view=None)


class RollbackButton(discord.ui.DynamicItem[discord.ui.Button], template=r"veritas:rollback:(?P<role>\d+)"):
    def __init__(self, role_id: int):
        super().__init__(discord.ui.Button(label="Откатить права", emoji="↩️", style=discord.ButtonStyle.danger,
                                           custom_id=f"veritas:rollback:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if member_level(interaction) < 5:
            await send_ephemeral(interaction, "Откатить права может только Совет.")
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            cog: AntiRaid = interaction.client.get_cog("AntiRaid")
            await interaction.response.defer(ephemeral=True, thinking=True)
            text = await cog.rollback_role(interaction.guild, self.role_id, interaction.user)
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc, "откат прав роли")


def rollback_view(role_id: int) -> discord.ui.View:
    view = SafeView(timeout=None)
    view.add_item(RollbackButton(role_id))
    return view


# ───────────────────── МОДУЛЬ ─────────────────────


class AntiRaid(commands.Cog):
    lockdown_group = app_commands.Group(name="lockdown", description="Локдаун сервера", guild_only=True)
    raid_group = app_commands.Group(name="raid", description="Защита сервера", guild_only=True)

    def __init__(self, bot: VeritasBot, *, clock=time.time):
        self.bot = bot
        self.clock = clock
        self.raid_cfg = config.RAID
        self.spam_cfg = config.SPAM
        self.nuke_cfg = config.NUKE
        self._build_detectors()
        self.state: dict = {"active": False, "raiders": []}
        self.recent_names: deque[tuple[float, str]] = deque(maxlen=50)
        self._last_offense: dict[int, float] = {}

    def _build_detectors(self) -> None:
        old = getattr(self, "detector", None)
        self.detector = RaidDetector(self.raid_cfg)
        if old is not None and old.active:
            self.detector.active, self.detector.started_at, self.detector.last_spike = (
                True, old.started_at, old.last_spike)
        self.spam = SpamTracker(self.spam_cfg)
        self.offenses = OffenseCounter(self.spam_cfg.offense_window_s)
        self.nuke = NukeDetector(self.nuke_cfg)

    async def cog_load(self) -> None:
        await self.load_config()
        saved = await self.bot.db.get_state(LOCK_STATE_KEY)
        if saved:
            self.state = saved
            if saved.get("active"):
                self.detector.start(saved.get("started", self.clock()))
                self.detector.last_spike = saved.get("last_spike") or saved.get("started")
        self.bot.add_view(LockdownView())
        self.bot.add_dynamic_items(RollbackButton)
        self.auto_lift_loop.start()

    async def cog_unload(self) -> None:
        self.auto_lift_loop.cancel()

    # ── конфигурация

    async def load_config(self) -> None:
        overrides = await self.bot.db.settings_with_prefix("cfg.")
        by_section: dict[str, dict[str, str]] = {s: {} for s in CFG_SECTIONS}
        for key, value in overrides.items():
            section, _, name = key.partition(".")
            if section in by_section:
                by_section[section][name] = value
        self.raid_cfg = override_dataclass(config.RAID, by_section["raid"])
        self.spam_cfg = override_dataclass(config.SPAM, by_section["spam"])
        self.nuke_cfg = override_dataclass(config.NUKE, by_section["nuke"])
        self._build_detectors()

    def config_params(self) -> dict[str, object]:
        params = {}
        for section, cfg in (("raid", self.raid_cfg), ("spam", self.spam_cfg), ("nuke", self.nuke_cfg)):
            for name, value in vars(cfg).items():
                if isinstance(value, (int, float, str, bool)):
                    params[f"{section}.{name}"] = value
        return params

    async def set_param(self, param: str, value: str) -> str:
        params = self.config_params()
        if param not in params:
            raise Denied("Нет такого параметра. Список — /raid config без аргументов.")
        current = params[param]
        value = value.strip()
        try:
            if isinstance(current, bool):
                if value.lower() not in ("true", "false", "1", "0", "on", "off", "да", "нет"):
                    raise ValueError
                value = "true" if value.lower() in ("true", "1", "on", "да") else "false"
            elif isinstance(current, int):
                if int(value) < 0:
                    raise ValueError
            elif isinstance(current, float):
                if float(value) < 0:
                    raise ValueError
            elif param == "raid.lockdown_action" and value not in ("timeout", "kick"):
                raise ValueError
        except ValueError:
            raise Denied(f"Неверное значение для {param}.") from None
        await self.bot.db.set_setting(f"cfg.{param}", value)
        await self.load_config()
        return f"{param}: {current} → {self.config_params()[param]}"

    # ── помощники

    def _council_mentions(self, guild: discord.Guild) -> str:
        gm = self.bot.guild_map(guild)
        return " ".join(r.mention for r in gm.roles((config.COUNCIL, config.HEAD))) if gm else ""

    async def _alert(self, guild: discord.Guild, embed: discord.Embed, *, view: discord.ui.View | None = None,
                     ping: bool = True) -> None:
        gm = self.bot.guild_map(guild)
        council = gm.channel("council") if gm else None
        if council is not None:
            try:
                kwargs = {"embed": embed}
                if view is not None:
                    kwargs["view"] = view
                if ping:
                    kwargs["content"] = self._council_mentions(guild) or None
                    kwargs["allowed_mentions"] = discord.AllowedMentions(roles=True)
                await council.send(**kwargs)
            except discord.HTTPException:
                log.warning("Не удалось отправить оповещение в #совет")
        if self.bot.logs is not None:
            self.bot.logs.send("bot", embed, force=True)
        log.warning("%s: %s", embed.title, embed.description)

    async def _save_state(self) -> None:
        self.state["last_spike"] = self.detector.last_spike
        await self.bot.db.set_state(LOCK_STATE_KEY, self.state)

    # ── вход

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.bot.is_home(member.guild):
            return
        now = self.clock()
        spike = self.detector.record_join(now)
        if spike and not self.detector.active:
            await self.start_lockdown(member.guild, f"{len(self.detector.joins)} входов за "
                                                    f"{self.raid_cfg.join_window_s} с", by=None)
        if self.detector.active:
            await self._lockdown_action(member)
            return
        await self._join_filters(member, now)

    async def _lockdown_action(self, member: discord.Member) -> None:
        if member.id not in self.state.setdefault("raiders", []):
            self.state["raiders"].append(member.id)
        await self._save_state()
        reason = "Veritas: локдаун (рейд)"
        try:
            if self.raid_cfg.lockdown_action == "kick":
                await member.kick(reason=reason)
            else:
                await member.timeout(timedelta(minutes=self.raid_cfg.lockdown_timeout_minutes), reason=reason)
        except discord.HTTPException:
            log.warning("Не удалось применить локдаун к %s", member.id)

    async def _join_filters(self, member: discord.Member, now: float) -> None:
        created = getattr(member, "created_at", None)
        age_h = (now - created.timestamp()) / 3600 if created else None
        names = [n for t, n in self.recent_names if now - t < 600]
        self.recent_names.append((now, member.name))
        s = suspicion(age_h, member.avatar is None, member.name, names, self.raid_cfg)
        young = (self.raid_cfg.new_account_hours > 0 and age_h is not None
                 and age_h < self.raid_cfg.new_account_hours)
        if not s.score:
            return
        if young or s.score >= self.raid_cfg.suspicion_threshold:
            minutes = self.raid_cfg.new_account_timeout_minutes
            try:
                await member.timeout(timedelta(minutes=minutes), reason="Veritas: подозрительный вход")
                action = f"тайм-аут {minutes} мин"
            except discord.HTTPException:
                action = "тайм-аут не выдан (нет прав)"
            color = "yellow"
        else:
            action, color = "только пометка", "blue"
        embed = make_embed("🛡 Подозрительный вход", f"{member.mention} (`{member.id}`) · {action}", color=color,
                           fields=[("Подозрительность", f"{s.score}: " + "; ".join(s.reasons), False)])
        if self.bot.logs is not None:
            self.bot.logs.send("bot", embed, force=True)

    # ── локдаун

    async def start_lockdown(self, guild: discord.Guild, reason: str, by: discord.abc.User | None) -> str:
        if self.detector.active and self.state.get("active"):
            return "Локдаун уже включён."
        now = self.clock()
        self.detector.start(now)
        self.state = {"active": True, "started": now, "reason": reason, "by": getattr(by, "id", None),
                      "raiders": [], "prev_invites_disabled": "INVITES_DISABLED" in guild.features,
                      "prev_verification": guild.verification_level.value}
        notes = []
        edit: dict = {}
        if self.raid_cfg.disable_invites and not self.state["prev_invites_disabled"]:
            edit["invites_disabled"] = True
        if self.raid_cfg.raise_verification and guild.verification_level < discord.VerificationLevel.high:
            edit["verification_level"] = discord.VerificationLevel.high
        if edit:
            try:
                await guild.edit(reason="Veritas: локдаун", **edit)
                if "invites_disabled" in edit:
                    notes.append("приглашения приостановлены")
                if "verification_level" in edit:
                    notes.append("уровень проверки поднят до «Высокий»")
            except discord.HTTPException:
                notes.append("не удалось изменить настройки сервера (нужно «Управление сервером»)")
        await self._save_state()
        who = by.mention if by else "автоматически"
        action = "тайм-аут" if self.raid_cfg.lockdown_action == "timeout" else "кик"
        embed = make_embed("🚨 ЛОКДАУН", f"Причина: {reason}. Включил: {who}.", color="red", fields=[
            ("Новые участники", f"{action} на время локдауна", False),
            ("Меры", ", ".join(notes) or "—", False),
            ("Авто-снятие", f"через {self.raid_cfg.auto_lift_minutes} мин без новых всплесков", False)])
        await self._alert(guild, embed, view=LockdownView())
        return "Локдаун включён."

    async def lift_lockdown(self, guild: discord.Guild, by: discord.abc.User | None, *, auto: bool = False) -> str:
        if not self.state.get("active"):
            self.detector.lift()
            return "Локдаун не активен."
        edit: dict = {}
        if self.raid_cfg.disable_invites and not self.state.get("prev_invites_disabled", False) \
                and "INVITES_DISABLED" in guild.features:
            edit["invites_disabled"] = False
        prev = self.state.get("prev_verification")
        if prev is not None and guild.verification_level.value != prev:
            edit["verification_level"] = discord.VerificationLevel(prev)
        note = ""
        if edit:
            try:
                await guild.edit(reason="Veritas: локдаун снят", **edit)
            except discord.HTTPException:
                note = " Настройки сервера вернуть не удалось — проверь вручную."
        raiders = len(self.state.get("raiders", []))
        self.detector.lift()
        self.state = {"active": False, "raiders": self.state.get("raiders", [])}
        await self._save_state()
        who = "автоматически (всплесков больше нет)" if auto else (by.mention if by else "—")
        await self._alert(guild, make_embed("🔓 Локдаун снят", f"Снял: {who}. Вошло во время локдауна: {raiders}."
                                            + note, color="green"), ping=False)
        return "Локдаун снят." + note

    async def kick_raiders(self, guild: discord.Guild, by: discord.abc.User) -> int:
        gm = self.bot.guild_map(guild)
        count = 0
        for uid in list(self.state.get("raiders", [])):
            member = guild.get_member(uid)
            if member is None or (gm and gm.level(member) >= 4):
                continue
            try:
                await member.kick(reason=f"Veritas: рейд, решение {by}")
                count += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)
        self.state["raiders"] = []
        await self._save_state()
        if self.bot.logs is not None:
            self.bot.logs.bot_event("👢 Рейдеры кикнуты", f"{by.mention}: кикнуто {count}.", color="red")
        return count

    @tasks.loop(seconds=30)
    async def auto_lift_loop(self) -> None:
        if self.detector.should_auto_lift(self.clock()):
            guild = self.bot.home_guild
            if guild is not None:
                await self.lift_lockdown(guild, None, auto=True)

    @auto_lift_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── анти-спам

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.bot.is_home(message.guild):
            return
        gm = self.bot.guild_map(message.guild)
        if gm is None or gm.level(message.author) >= 4:
            return  # Доверенные и выше защитой не наказываются
        now = self.clock()
        text_ok = self.bot.content_enabled
        reasons = self.spam.check(message.author.id, now, message.channel.id, message.content,
                                  mentions=len(message.raw_mentions) + len(message.raw_role_mentions),
                                  text_available=text_ok)
        if text_ok:
            channel_id = getattr(message.channel, "parent_id", None) or message.channel.id
            allow_links = gm.channel_key(channel_id) in self.spam_cfg.link_exempt_channels
            whitelist = tuple(self.spam_cfg.link_whitelist) + tuple(self.bot.settings.allowed_site_domains)
            violation = link_violation(message.content, whitelist, allow_links=allow_links)
            if violation:
                reasons.append(violation)
        if reasons:
            await self.punish(message, reasons, now)

    async def punish(self, message: discord.Message, reasons: list[str], now: float) -> str:
        member = message.author
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if now - self._last_offense.get(member.id, -1e9) < 3:
            return "deleted"  # та же серия — только удаляем
        self._last_offense[member.id] = now
        number = self.offenses.add(member.id, now)
        kind, seconds = ladder_action(number, self.spam_cfg)
        reason = "; ".join(reasons)
        if kind == "warn":
            try:
                await message.channel.send(f"{member.mention}, предупреждение: {reason}. Дальше — тайм-аут.",
                                           delete_after=15, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
            result = "предупреждение"
        else:
            try:
                await member.timeout(timedelta(seconds=seconds), reason=f"Veritas: анти-спам — {reason}")
                result = f"тайм-аут {seconds // 60} мин"
            except discord.HTTPException:
                result = "тайм-аут не выдан (нет прав)"
            self.spam.forget(member.id)
            await self.bot.db.execute(
                "INSERT INTO sanctions (user_id, kind, moderator_id, reason, ts) VALUES (?, ?, ?, ?, ?)",
                (member.id, "timeout", getattr(self.bot.user, "id", None), f"анти-спам: {reason}", now))
        if self.bot.logs is not None:
            self.bot.logs.send("bot", make_embed("🧹 Анти-спам", f"{member.mention} · {result}", color="yellow", fields=[
                ("Причина", reason, False), ("Канал", message.channel.mention, True),
                ("Нарушение за сутки", str(number), True)]), force=True)
        return kind

    # ── anti-nuke

    def _exempt(self, guild: discord.Guild, user_id: int | None) -> bool:
        if user_id is None or user_id == guild.owner_id or user_id == getattr(self.bot.user, "id", None):
            return True
        member = guild.get_member(user_id)
        gm = self.bot.guild_map(guild)
        return bool(member is not None and gm is not None and config.FOUNDER in gm.member_keys(member))

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        if not self.bot.is_home(guild):
            return
        changes_before = getattr(entry.changes, "before", None)
        changes_after = getattr(entry.changes, "after", None)
        kind = classify_audit(
            entry.action.name,
            target_is_everyone=getattr(entry.target, "id", None) == guild.id,
            extra_is_everyone=getattr(entry.extra, "id", None) == guild.id,
            permissions_changed=hasattr(changes_before, "permissions") or hasattr(changes_after, "permissions"))
        if kind is None:
            return
        await self.record_destructive(guild, entry.user_id, kind)

    async def record_destructive(self, guild: discord.Guild, actor_id: int | None, kind: str) -> bool:
        if self._exempt(guild, actor_id):
            return False
        kinds = self.nuke.record(actor_id, kind, time.monotonic())
        if kinds:
            await self.neutralize(guild, actor_id, kinds)
            return True
        return False

    async def neutralize(self, guild: discord.Guild, user_id: int, kinds: list[str]) -> list[str]:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        removed: list[str] = []
        notes: list[str] = []
        if member is not None:
            top = guild.me.top_role
            dangerous = [r for r in member.roles
                         if not r.is_default() and not r.managed and r < top
                         and any(getattr(r.permissions, p) for p in config.DANGEROUS_PERMS)]
            above = [r for r in member.roles if not r.is_default() and r >= top]
            if dangerous:
                await self.bot.db.execute(
                    "INSERT INTO role_snapshots (user_id, kind, roles_json, ts) VALUES (?, ?, ?, ?)",
                    (user_id, "nuke", "[" + ",".join(str(r.id) for r in dangerous) + "]", time.time()))
                try:
                    await member.remove_roles(*dangerous, reason="Veritas anti-nuke: массовые деструктивные действия")
                    removed = [r.name for r in dangerous]
                except discord.HTTPException:
                    notes.append("снять роли не удалось")
            if above:
                notes.append("есть роли выше роли бота — их бот снять не может: " + ", ".join(r.name for r in above))
            try:
                await member.timeout(timedelta(minutes=self.nuke_cfg.timeout_minutes),
                                     reason="Veritas anti-nuke")
                notes.append(f"тайм-аут {self.nuke_cfg.timeout_minutes // 60} ч")
            except discord.HTTPException:
                notes.append("тайм-аут выдать не удалось")
        else:
            notes.append("участник не найден на сервере")
        actions = ", ".join(DESTRUCTIVE_LABELS.get(k, k) for k in kinds)
        embed = make_embed("☢️ ANTI-NUKE", f"<@{user_id}> за {self.nuke_cfg.window_s} с: {actions}.", color="red",
                           fields=[("Сняты роли", ", ".join(removed) or "—", False),
                                   ("Меры", "; ".join(notes) or "—", False),
                                   ("Что дальше", "Бот ничего не восстанавливает сам. Проверь журнал аудита; "
                                                  "структуру вернёт /setup run. Роли — /restore после разбора.", False)])
        await self._alert(guild, embed)
        owner = guild.get_member(guild.owner_id)
        if owner is not None:
            try:
                await owner.send(embed=embed)
            except discord.HTTPException:
                pass
        return removed

    # ── защита структуры

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if not self.bot.is_home(after.guild) or after.managed:
            return
        gm = self.bot.guild_map(after.guild)
        founder = gm.role(config.FOUNDER) if gm else None
        if founder is not None and after.id == founder.id:
            return
        gained = [p for p in ("administrator", "manage_guild")
                  if getattr(after.permissions, p) and not getattr(before.permissions, p)]
        if not gained:
            return
        await self.bot.db.set_setting(f"rollback.{after.id}", str(before.permissions.value))
        names = {"administrator": "Администратор", "manage_guild": "Управление сервером"}
        embed = make_embed("⚠️ Опасные права у роли", f"Роль {after.mention} получила: "
                           + ", ".join(names[p] for p in gained) + ". Такие права должны быть только у Основателя.",
                           color="red")
        await self._alert(after.guild, embed, view=rollback_view(after.id))

    async def rollback_role(self, guild: discord.Guild, role_id: int, by: discord.abc.User) -> str:
        saved = await self.bot.db.get_setting(f"rollback.{role_id}")
        role = guild.get_role(role_id)
        if saved is None or role is None:
            return "Нечего откатывать: роль или сохранённые права не найдены."
        await role.edit(permissions=discord.Permissions(int(saved)), reason=f"Veritas: откат прав ({by})")
        await self.bot.db.set_setting(f"rollback.{role_id}", None)
        if self.bot.logs is not None:
            self.bot.logs.bot_event("↩️ Права роли откачены", f"{role.mention} — {by.mention}.", color="green")
        return f"Права роли {role.name} возвращены."

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        if not self.bot.is_home(after.guild):
            return
        gm = self.bot.guild_map(after.guild)
        key = gm.channel_key(after.id) if gm else None
        if key not in closed_keys():
            return
        everyone = after.guild.default_role
        was = before.overwrites_for(everyone).view_channel
        now = after.overwrites_for(everyone).view_channel
        if now is True and was is not True:
            await self._alert(after.guild, make_embed(
                "⚠️ @everyone видит закрытое", f"{after.mention}: @everyone получил право просмотра. "
                "Верни права через /setup run или вручную.", color="red"))

    # ── команды

    @lockdown_group.command(name="on", description="Включить локдаун")
    @min_level(5, "Локдауном управляет Совет.")
    async def lockdown_on(self, interaction: discord.Interaction, reason: str = "вручную") -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.start_lockdown(interaction.guild, reason, interaction.user),
                                        ephemeral=True)

    @lockdown_group.command(name="off", description="Снять локдаун")
    @min_level(5, "Локдауном управляет Совет.")
    async def lockdown_off(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.lift_lockdown(interaction.guild, interaction.user), ephemeral=True)

    @lockdown_group.command(name="status", description="Состояние локдауна")
    @min_level(5, "Локдауном управляет Совет.")
    async def lockdown_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.status_text(), ephemeral=True)

    def status_text(self) -> str:
        if not self.state.get("active"):
            return "🟢 Локдаун не активен."
        started = int(self.state.get("started", 0))
        return (f"🔴 Локдаун с <t:{started}:R>. Причина: {self.state.get('reason', '—')}. "
                f"Вошло за это время: {len(self.state.get('raiders', []))}.")

    @raid_group.command(name="config", description="Показать или изменить пороги защиты")
    @app_commands.describe(param="Параметр, например raid.join_threshold", value="Новое значение")
    @min_level(5, "Пороги защиты меняет Совет.")
    async def raid_config(self, interaction: discord.Interaction, param: str | None = None,
                          value: str | None = None) -> None:
        if param is None:
            lines = [f"`{k}` = {v}" for k, v in self.config_params().items()]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        if value is None:
            params = self.config_params()
            if param not in params:
                raise Denied("Нет такого параметра.")
            await interaction.response.send_message(f"`{param}` = {params[param]}", ephemeral=True)
            return
        text = await self.set_param(param, value)
        if self.bot.logs is not None:
            self.bot.logs.bot_event("🛡 Пороги защиты", f"{interaction.user.mention}: {text}", color="yellow")
        await interaction.response.send_message(text, ephemeral=True)

    @raid_config.autocomplete("param")
    async def _param_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=k, value=k) for k in self.config_params() if current in k][:25]


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(AntiRaid(bot))
