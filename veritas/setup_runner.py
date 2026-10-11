"""Настройка сервера: роли, права, категории, каналы, закреплённые тексты.

Идемпотентно: категории и каналы ищутся по сохранённому ID, затем по названию;
существующие обновляются, недостающие создаются. Ничего не удаляется.
Используется и консольной командой ``python -m veritas setup``, и ``/setup``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord

from . import config
from .config import CategorySpec, ChannelSpec, Settings
from .content import load, texts
from .db import Database
from .embeds import make_embed
from .guildmap import GuildMap, resolve_role_ids
from .logic.roles import plan_role_positions
from .permissions import BOT, EVERYONE, Overwrites, compute_overwrites

log = logging.getLogger("veritas.setup")

STEP_PAUSE_S = 0.6  # пауза между изменениями, чтобы не упираться в рейтлимиты


# ───────────────────── ПЛАН ─────────────────────


@dataclass
class Step:
    text: str
    run: Callable[[], Awaitable[None]] | None = None


@dataclass
class SetupReport:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    changes: int = 0
    aborted: bool = False

    def add(self, line: str) -> None:
        self.lines.append(line)
        log.info(line)

    def text(self) -> str:
        out = list(self.lines)
        if self.errors:
            out.append("")
            out.append("Ошибки:")
            out.extend(self.errors)
        return "\n".join(out)


def to_discord_overwrites(ow: Overwrites, guild: discord.Guild, gm: GuildMap,
                          extra: dict[str, discord.abc.Snowflake] | None = None
                          ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    result: dict = {}
    for target, perms in ow.items():
        if target == EVERYONE:
            obj = guild.default_role
        elif target == BOT:
            obj = guild.me
        elif extra and target in extra:
            obj = extra[target]
        else:
            obj = gm.role(target)
        if obj is None:
            continue
        result[obj] = discord.PermissionOverwrite(**perms)
    return result


def overwrite_signature(ow: dict) -> dict[int, tuple[int, int]]:
    sig = {}
    for target, o in ow.items():
        allow, deny = o.pair()
        if allow.value or deny.value:
            sig[target.id] = (allow.value, deny.value)
    return sig


def is_community(guild: discord.Guild) -> bool:
    return "COMMUNITY" in guild.features or "NEWS" in guild.features


# ───────────────────── ТЕКСТЫ ─────────────────────

Payload = dict  # content / embeds / view


def rules_payload(settings: Settings) -> Payload:
    embeds = [make_embed(texts.RULES_TITLE, load("rules.md"))]
    about = load("about.md")
    if settings.site_url:
        about += "\n\n" + texts.SITE_LINE.format(url=settings.site_url)
    embeds.append(make_embed(texts.ABOUT_TITLE, about, color="gold"))
    return {"content": None, "embeds": embeds}


def intake_payload(settings: Settings) -> Payload:
    return {"content": texts.INTAKE_TEMPLATE, "embeds": []}


def managed_messages() -> list[tuple[str, str, Callable[[Settings], Payload]]]:
    """(ключ сообщения, ключ канала, построитель). Панели добавляют модули бота."""
    items: list[tuple[str, str, Callable[[Settings], Payload]]] = [
        ("rules", "rules", rules_payload),
        ("intake", "intake", intake_payload),
    ]
    for key, channel_key, builder in PANEL_BUILDERS:
        items.append((key, channel_key, builder))
    return items


# Модули верификации и тикетов регистрируют здесь свои панели.
PANEL_BUILDERS: list[tuple[str, str, Callable[[Settings], Payload]]] = []


def register_panel(key: str, channel_key: str, builder: Callable[[Settings], Payload]) -> None:
    if all(k != key for k, _, _ in PANEL_BUILDERS):
        PANEL_BUILDERS.append((key, channel_key, builder))


async def upsert_message(db: Database, guild: discord.Guild, key: str,
                         channel: discord.abc.Messageable, payload: Payload, *, dry_run: bool,
                         pin: bool = True) -> str:
    """Опубликовать или отредактировать отслеживаемое сообщение. Возвращает строку отчёта."""
    row = await db.managed_message(key)
    msg = None
    if row is not None and row["channel_id"] == channel.id:
        try:
            msg = await channel.fetch_message(row["message_id"])
        except (discord.NotFound, discord.Forbidden):
            msg = None
    label = getattr(channel, "name", "?")
    if dry_run:
        return f"    ↻ обновит текст «{key}» в {label}" if msg else f"    + опубликует «{key}» в {label}"
    kwargs = {"content": payload.get("content"), "embeds": payload.get("embeds") or []}
    if payload.get("view") is not None:
        kwargs["view"] = payload["view"]
    if msg is not None:
        await msg.edit(**kwargs)
        line = f"    ↻ текст «{key}» обновлён"
    else:
        msg = await channel.send(**kwargs)
        await db.save_managed_message(key, channel.id, msg.id)
        line = f"    + опубликован «{key}»"
    if pin and not msg.pinned:
        try:
            await msg.pin(reason="Veritas: закреплённый текст")
        except discord.HTTPException:
            line += " (закрепить не удалось — нужно право «Закреплять сообщения»)"
    return line


# ───────────────────── НАСТРОЙКА ─────────────────────


class SetupRunner:
    def __init__(self, guild: discord.Guild, db: Database, settings: Settings, *, dry_run: bool):
        self.guild = guild
        self.db = db
        self.settings = settings
        self.dry_run = dry_run
        self.report = SetupReport()
        self.gm: GuildMap | None = None
        self.categories: dict[str, discord.CategoryChannel] = {}
        self.channels: dict[str, discord.abc.GuildChannel] = {}
        self.stored: dict[str, int] = {}

    async def _apply(self, steps: list[Step]) -> None:
        for step in steps:
            self.report.add(step.text)
            if step.run is None or self.dry_run:
                continue
            try:
                await step.run()
                self.report.changes += 1
            except discord.Forbidden:
                self.report.errors.append(f"✖ {step.text.strip()} — не хватает прав (подними роль бота)")
            except discord.HTTPException as exc:
                self.report.errors.append(f"✖ {step.text.strip()} — Discord: {exc.status} {exc.text}")
            await asyncio.sleep(STEP_PAUSE_S)

    async def run(self) -> SetupReport:
        g = self.guild
        r = self.report
        r.add(f"Сервер: {g.name}" + ("  [ПРОБНЫЙ ЗАПУСК — ничего не меняется]" if self.dry_run else ""))
        self.stored = await self.db.managed_channels()

        # ── роли
        r.add("")
        r.add("Роли…")
        cached = {k: int(v) for k, v in (await self.db.settings_with_prefix("role_id.")).items()
                  if v and v.isdigit()}
        ids, missing = resolve_role_ids([(x.id, x.name) for x in reversed(g.roles)], cached=cached)
        if missing:
            r.add("✖ Не найдены роли с ID: " + ", ".join(
                f"{config.ROLE_TITLES[k]} ({config.ROLE_IDS[k]})" for k in missing))
            r.add("  Проверь ID (ПКМ по роли → Копировать ID) в config.py и запусти снова.")
            r.aborted = True
            return r
        for key in config.ROLE_ORDER:
            if key in ids:
                r.add(f"  ✓ {config.ROLE_TITLES[key]}: {g.get_role(ids[key]).name}")
        if config.FOUNDER not in ids:
            r.add("  · Роль Основателя не найдена — владелец сервера и так имеет все права.")
        self.gm = GuildMap(g, ids, self.stored)
        await self._apply(self._plan_follower(ids))

        r.add("")
        r.add("Базовые права @everyone и права ролей…")
        await self._apply(self._plan_role_perms())
        self._warn_extra_admins()

        r.add("")
        r.add("Порядок ролей…")
        await self._apply(self._plan_role_order())

        r.add("")
        r.add("Категории и каналы…")
        for cat in config.STRUCTURE:
            await self._apply(self._plan_category(cat))
            for c in cat.channels:
                await self._apply(self._plan_channel(cat, c))

        r.add("")
        r.add("Закреплённые тексты и панели…")
        await self._messages()

        r.add("")
        if self.dry_run:
            r.add("Пробный запуск завершён. Для применения: setup без --dry-run или /setup run.")
        else:
            r.add(f"✔ Готово. Изменений: {r.changes}, ошибок: {len(r.errors)}.")
        return r

    # ── роль Последователя

    def _plan_follower(self, ids: dict[str, int]) -> list[Step]:
        if config.FOLL in ids:
            return []

        async def create():
            spec = config.FOLLOWER_CREATE
            role = await self.guild.create_role(name=spec["name"], colour=discord.Colour(spec["colour"]),
                                                hoist=spec["hoist"], reason="Veritas: роль Последователя")
            await self.db.set_setting("role_id.FOLL", str(role.id))
            self.gm.role_ids[config.FOLL] = role.id
            self.gm._key_by_role[role.id] = config.FOLL

        return [Step("  + создать роль 📖 Последователь", create)]

    # ── права ролей

    def _plan_role_perms(self) -> list[Step]:
        steps: list[Step] = []
        g = self.guild
        everyone = discord.Permissions(**config.SAFE_EVERYONE)
        if g.default_role.permissions != everyone:
            steps.append(Step("  ↻ права @everyone → безопасный набор",
                              lambda: g.default_role.edit(permissions=everyone,
                                                          reason="Veritas: базовые права")))
        else:
            steps.append(Step("  = @everyone без изменений"))
        me_top = g.me.top_role
        for key, perms in config.ROLE_PERMS.items():
            role = self.gm.role(key)
            if role is None:
                if key == config.FOLL and self.dry_run:
                    steps.append(Step("  ↻ права 📖 Последователь (после создания)"))
                continue
            want = discord.Permissions(**perms)
            if role.permissions == want:
                steps.append(Step(f"  = {role.name} без изменений"))
                continue
            if role >= me_top and g.owner_id != g.me.id:
                steps.append(Step(f"  ! {role.name}: роль выше роли бота — права не меняю, "
                                  f"поправь вручную или подними роль бота"))
                continue
            steps.append(Step(f"  ↻ права роли {role.name}",
                              _bind(role.edit, permissions=want, reason="Veritas: права роли")))
        return steps

    def _warn_extra_admins(self) -> None:
        founder = self.gm.role(config.FOUNDER)
        for role in self.guild.roles:
            if role.permissions.administrator and not role.managed and (founder is None or role.id != founder.id):
                self.report.add(f"  ⚠ у роли «{role.name}» есть «Администратор» — сними вручную, "
                                "если это не Основатель")

    # ── порядок ролей

    def _plan_role_order(self) -> list[Step]:
        g = self.guild
        current = {k: self.gm.role(k).position for k in config.ROLE_ORDER if self.gm.role(k) is not None}
        changes, problems = plan_role_positions(config.ROLE_ORDER, current, g.me.top_role.position)
        steps: list[Step] = []
        if changes:
            mapping = {self.gm.role(k): pos for k, pos in changes.items()}
            names = ", ".join(config.ROLE_TITLES[k] for k in changes)
            steps.append(Step(f"  ↻ выстроить роли: {names}",
                              _bind(g.edit_role_positions, positions=mapping,
                                    reason="Veritas: порядок ролей")))
        else:
            steps.append(Step("  = порядок ролей ниже роли бота верный"))
        for a, b in problems:
            steps.append(Step(f"  ! «{config.ROLE_TITLES[a]}» должна быть выше «{config.ROLE_TITLES[b]}» — "
                              "бот не может их двигать (выше его роли), перетащи вручную"))
        return steps

    # ── категории и каналы

    def _find_category(self, cat: CategorySpec) -> discord.CategoryChannel | None:
        cid = self.stored.get(cat.key)
        found = self.guild.get_channel(cid) if cid else None
        if isinstance(found, discord.CategoryChannel):
            return found
        return discord.utils.get(self.guild.categories, name=cat.name)

    def _find_channel(self, cat_obj, c: ChannelSpec):
        cid = self.stored.get(c.key)
        found = self.guild.get_channel(cid) if cid else None
        if found is not None and not isinstance(found, discord.CategoryChannel):
            return found
        if cat_obj is not None:
            found = discord.utils.get(cat_obj.channels, name=c.name)
            if found is not None:
                return found
        return next((x for x in self.guild.channels
                     if x.name == c.name and not isinstance(x, discord.CategoryChannel)), None)

    def _plan_category(self, cat: CategorySpec) -> list[Step]:
        want = to_discord_overwrites(compute_overwrites()[cat.key], self.guild, self.gm)
        existing = self._find_category(cat)
        if existing is not None:
            self.categories[cat.key] = existing

            async def remember():
                await self.db.save_managed_channel(cat.key, existing.id, None)

            if overwrite_signature(existing.overwrites) != overwrite_signature(want):
                async def update():
                    await existing.edit(overwrites=want, reason="Veritas: права категории")
                    await remember()
                return [Step(f"  ↻ {cat.name}: права", update)]
            if self.stored.get(cat.key) != existing.id:
                return [Step(f"  = {cat.name}", remember)]
            return [Step(f"  = {cat.name}")]

        async def create():
            new = await self.guild.create_category(cat.name, overwrites=want, reason="Veritas: настройка")
            self.categories[cat.key] = new
            await self.db.save_managed_channel(cat.key, new.id, None)

        return [Step(f"  + {cat.name}", create)]

    def _plan_channel(self, cat: CategorySpec, c: ChannelSpec) -> list[Step]:
        g = self.guild
        ow_abstract = compute_overwrites()[c.key]
        want = to_discord_overwrites(ow_abstract, g, self.gm)
        cat_obj = self.categories.get(cat.key)
        existing = self._find_channel(cat_obj, c)
        steps: list[Step] = []
        if c.kind == "news" and not is_community(g):
            steps.append(Step("    ⚠ " + texts.NEWS_FALLBACK_WARNING.format(name=c.name)))

        if existing is None:
            async def create():
                parent = self.categories.get(cat.key)
                new = await self._create_channel(parent, c, want)
                self.channels[c.key] = new
                await self.db.save_managed_channel(c.key, new.id, parent.id if parent else None)

            steps.append(Step(f"    + {c.name}", create))
            return steps

        self.channels[c.key] = existing
        changes: dict = {}
        notes: list[str] = []
        if overwrite_signature(existing.overwrites) != overwrite_signature(want):
            changes["overwrites"] = want
            notes.append("права")
        if cat_obj is not None and existing.category_id != cat_obj.id:
            changes["category"] = cat_obj
            notes.append(f"перенос в {cat.name}")
        if c.kind != "voice" and getattr(existing, "slowmode_delay", 0) != c.slow:
            changes["slowmode_delay"] = c.slow
            notes.append(f"медленный режим {c.slow} с")
        if c.topic and hasattr(existing, "topic") and not existing.topic:
            changes["topic"] = c.topic
            notes.append("описание")
        if (c.kind == "news" and is_community(g) and isinstance(existing, discord.TextChannel)
                and not existing.is_news()):
            changes["type"] = discord.ChannelType.news
            notes.append("тип «Объявления»")
        if c.kind == "forum" and isinstance(existing, discord.ForumChannel):
            have = {t.name for t in existing.available_tags}
            missing_tags = [t for t in config.FORUM_TAGS if t not in have]
            if missing_tags and len(existing.available_tags) + len(missing_tags) <= 20:
                changes["available_tags"] = list(existing.available_tags) + [
                    discord.ForumTag(name=t) for t in missing_tags]
                notes.append("теги форума")
        if c.kind == "forum" and not isinstance(existing, discord.ForumChannel):
            steps.append(Step(f"    ⚠ {c.name} — не форум (создан текстовым ранее); "
                              "чтобы получить форум, создай его вручную или удали старый канал"))

        async def remember():
            await self.db.save_managed_channel(c.key, existing.id, existing.category_id)

        if changes:
            async def update():
                await existing.edit(reason="Veritas: настройка", **changes)
                await remember()
            steps.append(Step(f"    ↻ {c.name}: " + ", ".join(notes), update))
        elif self.stored.get(c.key) != existing.id:
            steps.append(Step(f"    = {c.name}", remember))
        else:
            steps.append(Step(f"    = {c.name}"))
        return steps

    async def _create_channel(self, cat, c: ChannelSpec, ow):
        g = self.guild
        reason = "Veritas: настройка"
        if c.kind == "voice":
            return await g.create_voice_channel(c.name, category=cat, overwrites=ow, reason=reason)
        if c.kind == "forum":
            try:
                return await g.create_forum(
                    c.name, category=cat, overwrites=ow, topic=c.topic or "", slowmode_delay=c.slow,
                    available_tags=[discord.ForumTag(name=t) for t in config.FORUM_TAGS], reason=reason)
            except discord.HTTPException:
                self.report.add(f"    (форум недоступен — {c.name} создан текстовым)")
        if c.kind == "news" and is_community(g):
            try:
                return await g.create_text_channel(c.name, category=cat, overwrites=ow, topic=c.topic or "",
                                                   slowmode_delay=c.slow, news=True, reason=reason)
            except discord.HTTPException:
                self.report.add(f"    (для канала объявлений нужен режим «Сообщество» — {c.name} создан текстовым)")
        return await g.create_text_channel(c.name, category=cat, overwrites=ow, topic=c.topic or "",
                                           slowmode_delay=c.slow, reason=reason)

    # ── сообщения

    async def _messages(self) -> None:
        for key, channel_key, builder in managed_messages():
            channel = self.channels.get(channel_key)
            if channel is None:
                self.report.add(f"    · «{key}»: канал появится после применения настройки")
                continue
            try:
                line = await upsert_message(self.db, self.guild, key, channel, builder(self.settings),
                                            dry_run=self.dry_run)
                self.report.add(line)
                if not self.dry_run:
                    self.report.changes += 1
                    await asyncio.sleep(STEP_PAUSE_S)
            except discord.Forbidden:
                self.report.errors.append(f"✖ «{key}» — нет прав писать в {channel.name}")
            except discord.HTTPException as exc:
                self.report.errors.append(f"✖ «{key}» — Discord: {exc.status} {exc.text}")


def _bind(fn, /, **kwargs):
    async def call():
        await fn(**kwargs)
    return call


# ───────────────────── КОНСОЛЬ ─────────────────────


async def run_setup_cli(settings: Settings, *, dry_run: bool) -> int:
    """Настроить сервер и выйти (до запуска бота как сервиса)."""
    db = Database(settings.db_path)
    await db.connect()
    client = discord.Client(intents=discord.Intents.default())
    code = 1
    try:
        await client.login(settings.discord_token)
        connect = asyncio.create_task(client.connect(reconnect=False))
        ready = asyncio.create_task(client.wait_until_ready())
        done, _ = await asyncio.wait({connect, ready}, return_when=asyncio.FIRST_COMPLETED)
        if connect in done:
            connect.result()  # пробросить ошибку соединения
        guild = client.get_guild(settings.guild_id)
        if guild is None:
            print("✖ Бот не найден на сервере с таким ID. Пригласи его и проверь GUILD_ID.")
        else:
            runner = SetupRunner(guild, db, settings, dry_run=dry_run)
            report = await runner.run()
            print(report.text())
            code = 0 if not report.aborted and not report.errors else 2
    except discord.LoginFailure:
        print("✖ Неверный токен. Developer Portal → Bot → Reset Token.")
    except discord.Forbidden:
        print("✖ Не хватает прав. Для первичной настройки дай боту «Администратор» и подними его роль.")
    finally:
        await client.close()
        await db.close()
    return code
