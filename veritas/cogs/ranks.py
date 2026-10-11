"""Иерархия и наказания: /rank, /liar, /exile, /restore, /timeout, /warn, активность."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config
from ..clearance import (
    NEXT_RANK,
    PREV_RANK,
    can_mark,
    can_set_rank,
    can_timeout,
    clearance_level,
    member_rank,
    plan_exile,
    plan_liar,
    plan_restore,
    plan_set_rank,
    rank_index,
)
from ..content import texts
from ..embeds import make_embed
from ..guildmap import GuildMap
from ..logic.activity import ActivityCounter
from ..logic.verification import human_duration
from ..ui import Denied, min_level

if TYPE_CHECKING:
    from ..bot import VeritasBot

log = logging.getLogger("veritas.ranks")

RANK_CHOICES = [app_commands.Choice(name=config.ROLE_TITLES[k], value=k)
                for k in (config.HEAD, config.COUNCIL, config.ARCH, config.TRUST, config.INIT, config.FOLL,
                          config.STRANGER)]


def titles(keys) -> str:
    ordered = [k for k in config.ROLE_ORDER if k in set(keys)]
    return ", ".join(config.ROLE_TITLES[k] for k in ordered) or "—"


class Ranks(commands.Cog):
    rank_group = app_commands.Group(name="rank", description="Допуски и ранги", guild_only=True)

    def __init__(self, bot: VeritasBot):
        self.bot = bot
        self.activity = ActivityCounter()
        self.cfg = config.RANKS

    async def cog_load(self) -> None:
        self.flush_activity.start()

    async def cog_unload(self) -> None:
        self.flush_activity.cancel()
        await self.flush()

    # ── помощники

    def gm(self, guild) -> GuildMap:
        gm = self.bot.guild_map(guild)
        if gm is None:
            raise Denied("Сервер не настроен.")
        return gm

    def _check_target(self, guild, actor, target) -> None:
        if target.bot:
            raise Denied("Ботов это не касается.")
        if target.id == actor.id:
            raise Denied("С собой так нельзя.")
        me = guild.me
        if target.id == guild.owner_id:
            raise Denied("Владельца сервера трогать нельзя.")
        if getattr(target, "top_role", None) is not None and target.top_role >= me.top_role:
            raise Denied("Роль этого человека выше роли бота — бот не может её менять.")

    async def apply(self, gm: GuildMap, member, remove: set[str], add: set[str], reason: str) -> None:
        remove_roles = [r for r in gm.roles(remove) if r in member.roles]
        add_roles = [r for r in gm.roles(add) if r not in member.roles]
        missing = [k for k in add if gm.role(k) is None]
        if missing:
            raise Denied("Не найдены роли: " + titles(missing) + ". Запусти /setup run.")
        if remove_roles:
            await member.remove_roles(*remove_roles, reason=reason)
        if add_roles:
            await member.add_roles(*add_roles, reason=reason)

    async def journal(self, guild, title: str, actor, target, what: str, reason: str, color: str = "blue") -> None:
        embed = make_embed(title, None, color=color, fields=[
            ("Кто", actor.mention, True), ("Кому", f"{target.mention} (`{target.id}`)", True),
            ("Что", what, False), ("Почему", reason or "не указано", False),
            ("Когда", f"<t:{int(time.time())}:f>", True)])
        gm = self.bot.guild_map(guild)
        court = gm.channel("court") if gm else None
        if court is not None:
            try:
                await court.send(embed=embed)
            except discord.HTTPException:
                log.warning("Не удалось записать в журнал #суд-и-допуски")
        self.bot.log_event(title, f"{actor.mention} → {target.mention}: {what}", color=color)

    async def sanction(self, user_id: int, kind: str, moderator_id: int | None, reason: str | None) -> None:
        await self.bot.db.execute(
            "INSERT INTO sanctions (user_id, kind, moderator_id, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, moderator_id, reason, time.time()))

    async def active_days(self, user_id: int) -> tuple[int, int]:
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS days, COALESCE(SUM(messages), 0) AS total FROM activity WHERE user_id = ?",
            (user_id,))
        return row["days"], row["total"]

    # ── ранги (ядро)

    async def set_rank(self, guild, actor, target, new_rank: str, reason: str) -> str:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        actor_keys, target_keys = gm.member_keys(actor), gm.member_keys(target)
        ok, why = can_set_rank(actor_keys, target_keys, new_rank, self.bot.settings.head_can_appoint_council)
        if not ok:
            raise Denied(why)
        old = member_rank(target_keys)
        remove, add = plan_set_rank(target_keys, new_rank)
        await self.apply(gm, target, remove, add, f"Veritas: {actor} — {reason}")
        what = f"{config.ROLE_TITLES.get(old, 'нет ранга') if old else 'нет ранга'} → {config.ROLE_TITLES[new_rank]}"
        up = rank_index(new_rank) > rank_index(old)
        await self.journal(guild, "📜 Повышение" if up else "📜 Понижение", actor, target, what, reason,
                           color="green" if up else "yellow")
        hint = ""
        if new_rank == config.INIT:
            days, _ = await self.active_days(target.id)
            if days < self.cfg.active_days_hint:
                hint = f"\n⚠️ Активных дней: {days} (рекомендуется {self.cfg.active_days_hint})."
        return f"{target.mention}: {what}.{hint}"

    async def promote(self, guild, actor, target, reason: str) -> str:
        current = member_rank(self.gm(guild).member_keys(target))
        nxt = NEXT_RANK.get(current)
        if nxt is None:
            raise Denied("Выше повышать некуда.")
        return await self.set_rank(guild, actor, target, nxt, reason)

    async def demote(self, guild, actor, target, reason: str) -> str:
        current = member_rank(self.gm(guild).member_keys(target))
        prev = PREV_RANK.get(current)
        if prev is None:
            raise Denied("Ниже понижать некуда. Для наказаний — /liar или /exile.")
        return await self.set_rank(guild, actor, target, prev, reason)

    # ── метки (ядро)

    async def _mark_checks(self, guild, actor, target) -> tuple[GuildMap, set[str]]:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        actor_keys, target_keys = gm.member_keys(actor), gm.member_keys(target)
        if clearance_level(actor_keys) < 5:
            raise Denied("Метки накладывает Совет.")
        ok, why = can_mark(actor_keys, target_keys)
        if not ok:
            raise Denied(why)
        return gm, target_keys

    async def _snapshot(self, user_id: int, kind: str, keys: set[str]) -> None:
        await self.bot.db.execute(
            "INSERT INTO role_snapshots (user_id, kind, roles_json, ts) VALUES (?, ?, ?, ?)",
            (user_id, kind, json.dumps(sorted(keys)), time.time()))

    async def liar(self, guild, actor, target, reason: str) -> str:
        gm, keys = await self._mark_checks(guild, actor, target)
        if config.EXILE in keys:
            raise Denied("Человек уже отлучён.")
        remove, add = plan_liar(keys)
        await self._snapshot(target.id, "liar", remove)
        await self.apply(gm, target, remove, add, f"Veritas: Лжец — {reason}")
        await self.bot.db.execute("INSERT OR REPLACE INTO liars (user_id, ts) VALUES (?, ?)", (target.id, time.time()))
        await self.sanction(target.id, "liar", actor.id, reason)
        await _dm(target, texts.LIAR_DM.format(reason=reason))
        await self.journal(guild, "🐍 Метка Лжеца", actor, target, f"сняты: {titles(remove)}", reason, color="red")
        return f"{target.mention} — 🐍 Лжец. Снято: {titles(remove)}. Вернуть — /restore."

    async def exile(self, guild, actor, target, reason: str) -> str:
        gm, keys = await self._mark_checks(guild, actor, target)
        remove, add = plan_exile(keys, config.MANAGED_ROLES)
        await self._snapshot(target.id, "exile", remove)
        await self.apply(gm, target, remove, add, f"Veritas: отлучение — {reason}")
        await self.bot.db.execute("INSERT OR REPLACE INTO exiled (user_id, ts) VALUES (?, ?)",
                                  (target.id, time.time()))
        await self.sanction(target.id, "exile", actor.id, reason)
        await _dm(target, texts.EXILE_DM.format(reason=reason))
        await self.journal(guild, "🚫 Отлучение", actor, target, f"сняты: {titles(remove)}", reason, color="red")
        return f"{target.mention} — 🚫 Отлучён. Снято: {titles(remove)}. Вернуть — /restore."

    async def restore(self, guild, actor, target, reason: str) -> str:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        actor_keys, target_keys = gm.member_keys(actor), gm.member_keys(target)
        if clearance_level(actor_keys) < 5:
            raise Denied("Снимать метки может Совет.")
        rows = await self.bot.db.fetchall(
            "SELECT roles_json FROM role_snapshots WHERE user_id = ? AND kind IN ('liar', 'exile')", (target.id,))
        snapshot: set[str] = set()
        for r in rows:
            snapshot |= set(json.loads(r["roles_json"]))
        if not (target_keys & config.MARK_ROLES) and not rows:
            raise Denied("На человеке нет меток и нечего восстанавливать.")
        remove, add = plan_restore(target_keys, snapshot)
        top_back = member_rank(add)
        if rank_index(member_rank(actor_keys)) <= rank_index(top_back):
            raise Denied("Ты не можешь вернуть роль, равную своей или выше.")
        add = {k for k in add if gm.role(k) is not None}  # ролей, которых уже нет, не возвращаем
        await self.apply(gm, target, remove, add, f"Veritas: восстановление — {reason}")
        await self.bot.db.execute("DELETE FROM role_snapshots WHERE user_id = ? AND kind IN ('liar', 'exile')",
                                  (target.id,))
        await self.bot.db.execute("DELETE FROM exiled WHERE user_id = ?", (target.id,))
        await self.bot.db.execute("DELETE FROM liars WHERE user_id = ?", (target.id,))
        await self.sanction(target.id, "restore", actor.id, reason)
        await _dm(target, texts.RESTORE_DM.format(reason=reason))
        await self.journal(guild, "↩️ Восстановление", actor, target,
                           f"сняты метки: {titles(remove)}; возвращено: {titles(add)}", reason, color="green")
        return f"{target.mention}: метки сняты, возвращено: {titles(add)}."

    # ── тайм-ауты и предупреждения (ядро)

    async def timeout(self, guild, actor, target, minutes: int, reason: str) -> str:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        ok, why = can_timeout(gm.member_keys(actor), gm.member_keys(target), minutes * 60,
                              self.cfg.trusted_timeout_max_s, self.cfg.discord_timeout_max_s)
        if not ok:
            raise Denied(why)
        await target.timeout(timedelta(minutes=minutes), reason=f"Veritas: {actor} — {reason}")
        await self.sanction(target.id, "timeout", actor.id, reason)
        duration = human_duration(minutes * 60)
        await _dm(target, texts.TIMEOUT_DM.format(duration=duration, reason=reason))
        await self.journal(guild, "🔇 Тайм-аут", actor, target, duration, reason, color="yellow")
        return f"{target.mention}: тайм-аут {duration}."

    async def untimeout(self, guild, actor, target, reason: str) -> str:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        ok, why = can_timeout(gm.member_keys(actor), gm.member_keys(target), 1,
                              self.cfg.trusted_timeout_max_s, self.cfg.discord_timeout_max_s)
        if not ok:
            raise Denied(why)
        await target.timeout(None, reason=f"Veritas: {actor} — {reason}")
        await self.journal(guild, "🔊 Тайм-аут снят", actor, target, "снят", reason, color="green")
        return f"{target.mention}: тайм-аут снят."

    async def warn(self, guild, actor, target, reason: str) -> str:
        self._check_target(guild, actor, target)
        gm = self.gm(guild)
        actor_keys, target_keys = gm.member_keys(actor), gm.member_keys(target)
        if clearance_level(actor_keys) < 4:
            raise Denied("Предупреждения выносят Доверенные и выше.")
        if rank_index(member_rank(actor_keys)) <= rank_index(member_rank(target_keys)):
            raise Denied("Нельзя трогать того, кто равен тебе или выше.")
        await self.sanction(target.id, "warn", actor.id, reason)
        count = await self.warn_count(target.id)
        await _dm(target, texts.WARN_DM.format(reason=reason, count=count))
        await self.journal(guild, "⚠️ Предупреждение", actor, target, f"всего предупреждений: {count}", reason,
                           color="yellow")
        reply = f"{target.mention}: предупреждение №{count}."
        if count >= self.cfg.warn_hint_threshold:
            reply += f"\n{texts.LADDER}"
            council = gm.channel("council")
            if council is not None:
                try:
                    await council.send(embed=make_embed(
                        "⚖️ Повод для разбора", f"У {target.mention} уже {count} предупреждения. {texts.LADDER}",
                        color="yellow"))
                except discord.HTTPException:
                    pass
        return reply

    async def warn_count(self, user_id: int) -> int:
        return await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM sanctions WHERE user_id = ? AND kind = 'warn'", (user_id,), 0)

    async def info(self, guild, target) -> discord.Embed:
        gm = self.gm(guild)
        keys = gm.member_keys(target)
        days, total = await self.active_days(target.id)
        warns = await self.warn_count(target.id)
        marks = []
        if config.LIAR in keys or await self.bot.db.is_liar(target.id):
            marks.append("🐍 Лжец")
        if config.EXILE in keys or await self.bot.db.is_exiled(target.id):
            marks.append("🚫 Отлучён")
        joined = getattr(target, "joined_at", None)
        rank = member_rank(keys)
        hint = ""
        if rank == config.FOLL:
            need = self.cfg.active_days_hint
            hint = ("✅ Можно рассматривать повышение до Посвящённого." if days >= need
                    else f"До подсказки о повышении: {days}/{need} активных дней.")
        fields = [
            ("Допуск", str(clearance_level(keys)), True),
            ("Ранг", config.ROLE_TITLES.get(rank, "—") if rank else "—", True),
            ("Роли", titles(keys), False),
            ("На сервере с", f"<t:{int(joined.timestamp())}:D>" if joined else "неизвестно", True),
            ("Активных дней", f"{days} (сообщений: {total})", True),
            ("Метки", ", ".join(marks) or "нет", True),
            ("Предупреждений", str(warns), True),
        ]
        if hint:
            fields.append(("Подсказка", hint, False))
        return make_embed(f"📇 {target.display_name}", None, color="gold", fields=fields, footer=f"ID: {target.id}")

    # ── активность

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not self.bot.is_home(message.guild):
            return
        self.activity.add(message.author.id, time.time())

    @tasks.loop(seconds=60)
    async def flush_activity(self) -> None:
        await self.flush()

    async def flush(self) -> None:
        for user_id, day, n in self.activity.drain():
            await self.bot.db.execute(
                "INSERT INTO activity (user_id, day, messages) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET messages = messages + excluded.messages",
                (user_id, day, n))

    # ── команды

    @rank_group.command(name="promote", description="Повысить на ступень")
    @min_level(4, "Повышают Доверенные и выше.")
    async def cmd_promote(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        await interaction.response.send_message(await self.promote(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @rank_group.command(name="demote", description="Понизить на ступень")
    @min_level(4, "Понижают Доверенные и выше.")
    async def cmd_demote(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        await interaction.response.send_message(await self.demote(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @rank_group.command(name="set", description="Назначить ранг")
    @app_commands.choices(role=RANK_CHOICES)
    @min_level(4, "Ранги назначают Доверенные и выше.")
    async def cmd_set(self, interaction: discord.Interaction, user: discord.Member,
                      role: app_commands.Choice[str], reason: str) -> None:
        await interaction.response.send_message(
            await self.set_rank(interaction.guild, interaction.user, user, role.value, reason), ephemeral=True)

    @rank_group.command(name="info", description="Допуск, роли, активность и метки")
    async def cmd_info(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        target = user or interaction.user
        gm = self.gm(interaction.guild)
        if target.id != interaction.user.id and gm.level(interaction.user) < 4:
            raise Denied("Чужие досье смотрят Доверенные и выше.")
        await interaction.response.send_message(embed=await self.info(interaction.guild, target), ephemeral=True)

    @app_commands.command(name="liar", description="Метка Лжеца: снять роли выше Последователя")
    @app_commands.guild_only()
    @min_level(5, "Метки накладывает Совет.")
    async def cmd_liar(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        await interaction.response.send_message(await self.liar(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @app_commands.command(name="exile", description="Отлучение: оставить только «Отлучённый»")
    @app_commands.guild_only()
    @min_level(5, "Отлучает Совет.")
    async def cmd_exile(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        await interaction.response.send_message(await self.exile(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @app_commands.command(name="restore", description="Снять метку и вернуть сохранённые роли")
    @app_commands.guild_only()
    @min_level(5, "Метки снимает Совет.")
    async def cmd_restore(self, interaction: discord.Interaction, user: discord.Member,
                          reason: str = "решение Совета") -> None:
        await interaction.response.send_message(await self.restore(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @app_commands.command(name="timeout", description="Тайм-аут (Доверенные — до 24 ч)")
    @app_commands.guild_only()
    @app_commands.describe(minutes="Длительность в минутах")
    @min_level(4, "Тайм-ауты выдают Доверенные и выше.")
    async def cmd_timeout(self, interaction: discord.Interaction, user: discord.Member,
                          minutes: app_commands.Range[int, 1, 40320], reason: str) -> None:
        await interaction.response.send_message(
            await self.timeout(interaction.guild, interaction.user, user, minutes, reason), ephemeral=True)

    @app_commands.command(name="untimeout", description="Снять тайм-аут")
    @app_commands.guild_only()
    @min_level(4, "Тайм-ауты снимают Доверенные и выше.")
    async def cmd_untimeout(self, interaction: discord.Interaction, user: discord.Member,
                            reason: str = "снят досрочно") -> None:
        await interaction.response.send_message(
            await self.untimeout(interaction.guild, interaction.user, user, reason), ephemeral=True)

    @app_commands.command(name="warn", description="Вынести предупреждение")
    @app_commands.guild_only()
    @min_level(4, "Предупреждения выносят Доверенные и выше.")
    async def cmd_warn(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        await interaction.response.send_message(await self.warn(interaction.guild, interaction.user, user, reason),
                                                ephemeral=True)

    @app_commands.command(name="warns", description="Предупреждения и наказания участника")
    @app_commands.guild_only()
    @min_level(4, "Это видят Доверенные и выше.")
    async def cmd_warns(self, interaction: discord.Interaction, user: discord.Member) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT kind, moderator_id, reason, ts FROM sanctions WHERE user_id = ? ORDER BY ts DESC LIMIT 15",
            (user.id,))
        names = {"warn": "⚠️ предупреждение", "timeout": "🔇 тайм-аут", "liar": "🐍 Лжец", "exile": "🚫 отлучение",
                 "restore": "↩️ восстановление"}
        lines = [f"<t:{int(r['ts'])}:d> {names.get(r['kind'], r['kind'])} — "
                 f"{('<@' + str(r['moderator_id']) + '>') if r['moderator_id'] else 'бот'}: {r['reason'] or '—'}"
                 for r in rows]
        count = await self.warn_count(user.id)
        text = f"**{user.display_name}** — предупреждений: {count}\n" + ("\n".join(lines) or "Записей нет.")
        if count >= self.cfg.warn_hint_threshold:
            text += f"\n\n{texts.LADDER}"
        await interaction.response.send_message(text[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def _dm(member, text: str) -> None:
    try:
        await member.send(text)
    except (discord.HTTPException, AttributeError):
        pass


async def setup(bot: VeritasBot) -> None:
    await bot.add_cog(Ranks(bot))
