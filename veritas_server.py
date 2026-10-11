"""
VERITAS — настройка каналов и прав доступа под уже созданные роли.

Что делает:
  • берёт твои роли по ID (не переименовывает и не перекрашивает их);
  • выставляет ролям безопасные права (администратор — только у Основателя);
  • создаёт 6 категорий и 16 каналов, у КАЖДОГО канала свои права:
    кто видит, кто пишет, реакции, ветки, файлы, медленный режим;
  • ничего не удаляет; повторный запуск безопасен (существующее обновляется).

Запуск:
    pip install -U discord.py
    python veritas_server.py
"""

import getpass
import discord

# ───────────────────── ТВОИ РОЛИ (ID) ─────────────────────

HEAD = "Глава Совета"
COUNCIL = "Член Совета"
ARCH = "Архивариус"
TRUST = "Доверенный"
INIT = "Посвящённый"
FOLL = "Последователь"
STRANGER = "Незнакомец"
LIAR = "Лжец"
EXILE = "Отлучённый"
FOUNDER = "Основатель"

ROLE_IDS = {
    HEAD: 1558283519849930782,
    COUNCIL: 1558284211637461032,
    ARCH: 1558284385508134993,
    TRUST: 1558284608842502154,
    INIT: 1558284784449622107,
    STRANGER: 1558284987319455744,
    LIAR: 1558285108220399717,
    EXILE: 1558285281529040956,
}

# ID этих ролей не указаны — ищутся по названию.
# Последователь, если не найдётся, будет создан.
# Основатель необязателен: у владельца сервера и так все права.
FIND_BY_NAME = {
    FOLL: "последоват",
    FOUNDER: "основател",
}

# ───────────────────── ПРАВА РОЛЕЙ (на весь сервер) ─────────────────────
# «Администратор», «Управление сервером» и вебхуки — только у Основателя.
# Удалять и создавать каналы может только Глава Совета.

ROLE_PERMS = {
    FOUNDER: dict(administrator=True),
    HEAD: dict(
        manage_channels=True, manage_roles=True, manage_messages=True, manage_threads=True,
        manage_nicknames=True, kick_members=True, ban_members=True, moderate_members=True,
        view_audit_log=True, mention_everyone=True, manage_events=True,
        manage_expressions=True, move_members=True, mute_members=True, deafen_members=True,
    ),
    COUNCIL: dict(
        manage_roles=True, manage_messages=True, manage_threads=True, manage_nicknames=True,
        kick_members=True, ban_members=True, moderate_members=True, view_audit_log=True,
        move_members=True, mute_members=True,
    ),
    ARCH: dict(manage_messages=True, manage_threads=True),
    TRUST: dict(moderate_members=True, manage_messages=True, manage_nicknames=True,
                mute_members=True),
    INIT: {},
    FOLL: {},
    STRANGER: {},
    LIAR: {},
    EXILE: {},
}

SAFE_EVERYONE = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True, add_reactions=True,
    embed_links=True, attach_files=True, use_external_emojis=True, connect=True, speak=True,
    use_voice_activation=True, stream=True, use_application_commands=True,
    send_messages_in_threads=True, create_public_threads=True, change_nickname=True,
)

# ───────────────────── УРОВНИ ДОПУСКА ─────────────────────

ALL = None                              # видят все (кроме Отлучённых)
L5 = [FOUNDER, HEAD, COUNCIL]           # Ядро
L4 = L5 + [ARCH, TRUST]                 # Тайны
L3 = L4 + [INIT]                        # Досье
L2 = L3 + [FOLL]                        # Библиотека
TOP = [FOUNDER, HEAD]                   # Основатель и Глава Совета


def ch(kind, name, topic, view, write, *, hide=(), show=(), slow=0,
       reactions=True, threads=True, roles=None):
    """
    view      — кто видит (ALL или список ролей)
    write     — "all" (все, кто видит) или список ролей, которые могут писать
                (для форума — кто создаёт публикации; отвечать могут все, кто видит)
    hide/show — точечно скрыть/показать канал ролям
    slow      — медленный режим, секунд
    reactions — могут ли читатели ставить реакции
    threads   — можно ли создавать ветки
    roles     — особые ограничения для отдельных ролей
    """
    return dict(kind=kind, name=name, topic=topic, view=view, write=write, hide=hide,
                show=show, slow=slow, reactions=reactions, threads=threads, roles=roles or {})


# ───────────────────── КАНАЛЫ (16) ─────────────────────

STRUCTURE = [
    ("🚪 ВХОД", ALL, [
        ch("text", "📜┃правила", "Правила, о секте, ссылка на сайт.",
           ALL, TOP, reactions=False, threads=False),
        ch("text", "✅┃верификация", "Нажми кнопку и заполни анкету, чтобы войти.",
           ALL, TOP, threads=False,
           hide=[FOLL, INIT, ARCH],                     # прошедшие верификацию его не видят
           show=[TRUST, COUNCIL, HEAD, FOUNDER]),       # проверяющие видят всегда
    ]),
    ("💬 ОБЩИНА", L2, [
        ch("news", "📢┃новости", "Объявления и вскрытые тайны.",
           L2, TOP, threads=False),
        ch("text", "💬┃общий-чат", "Общение.",
           L2, "all",
           roles={LIAR: dict(attach_files=False, embed_links=False,
                             create_public_threads=False)}),
        ch("voice", "🎙┃голосовой", None,
           L2, "all", roles={LIAR: dict(stream=False)}),
        ch("text", "🎫┃заявки", "Тикеты: допуск, жалоба, вопрос Совету.",
           L2, TOP, reactions=False, threads=False),
    ]),
    ("🗂 АРХИВ", L3, [
        ch("text", "📚┃архив", "Лента обновлений архива на сайте.",
           L3, [FOUNDER, HEAD, ARCH], threads=False),
        ch("text", "📥┃приём-материалов", "Сдавай информацию по шаблону из закрепа.",
           L3, "all", slow=60),
        ch("forum", "🔎┃расследования", "Одна публикация = одно дело.",
           L3, [FOUNDER, HEAD, COUNCIL, ARCH]),
    ]),
    ("🔒 ТАЙНЫ", L4, [
        ch("text", "📋┃анкеты", "Анкеты новичков. Решают Доверенные и выше (кнопки бота).",
           L4, [], reactions=False, threads=False),
        ch("text", "🕳┃тайны", "Обсуждение секретных материалов. Слив = Лжец и суд.",
           L4, "all"),
        ch("text", "🗝┃хранилище", "Ссылки на защищённые файлы. Ключи выдаёт Архивариус.",
           L4, [FOUNDER, HEAD, COUNCIL, ARCH], threads=False),
    ]),
    ("⚫ СОВЕТ", L5, [
        ch("text", "⚫┃совет", "Обсуждения и голосования Совета.", L5, "all"),
        ch("text", "⚖️┃суд-и-допуски", "Разбор нарушителей и журнал выдачи допусков.", L5, "all"),
        ch("text", "📊┃логи", "Логи ботов.", L5, [], reactions=False, threads=False),
    ]),
    ("🚫 ОСОБОЕ", L5 + [EXILE], [
        ch("text", "🚫┃апелляции", "Здесь Отлучённые могут просить о возвращении.",
           L5 + [EXILE], "all", slow=600, threads=False),
    ]),
]

FORUM_TAGS = ["🟡 Открыто", "🔎 Проверяется", "✅ Подтверждено", "❌ Опровергнуто", "🔒 Закрыто"]

INTAKE_TEMPLATE = (
    "📥 **Как сдать материал**\n"
    "Скопируй, заполни и отправь сюда одним сообщением:\n"
    "```\n"
    "О ком / о чём:\n"
    "Что узнал:\n"
    "Доказательства (ссылка / скрин):\n"
    "Откуда информация:\n"
    "Предлагаемый уровень (2-5):\n"
    "```\n"
    "Архивариус проверит материал в течение 48 часов.\n"
    "⚠️ Только игровая информация: без реальных имён, фото, адресов и личных переписок. "
    "Ложь в материалах = метка Лжец."
)


# ───────────────────── ЛОГИКА ─────────────────────

def category_overwrites(guild, R, viewers):
    ow = {}
    if viewers is ALL:
        ow[guild.default_role] = discord.PermissionOverwrite(view_channel=True)
        if EXILE in R:
            ow[R[EXILE]] = discord.PermissionOverwrite(view_channel=False)
    else:
        ow[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        for n in viewers:
            if n in R:
                ow[R[n]] = discord.PermissionOverwrite(view_channel=True)
    return ow


def channel_overwrites(guild, R, c):
    ow = {}

    def o(role):
        return ow.setdefault(role, discord.PermissionOverwrite())

    every = o(guild.default_role)

    # кто видит
    if c["view"] is ALL:
        every.update(view_channel=True)
        if EXILE in R:
            o(R[EXILE]).update(view_channel=False)
    else:
        every.update(view_channel=False)
        for n in c["view"]:
            if n in R:
                o(R[n]).update(view_channel=True)
    for n in c["hide"]:
        if n in R:
            o(R[n]).update(view_channel=False)
    for n in c["show"]:
        if n in R:
            o(R[n]).update(view_channel=True)

    # кто пишет
    if c["write"] != "all":
        every.update(send_messages=False)
        for n in c["write"]:
            if n in R:
                o(R[n]).update(view_channel=True, send_messages=True)

    # ветки
    if not c["threads"] and c["kind"] != "forum":
        every.update(create_public_threads=False, create_private_threads=False)

    # реакции
    if not c["reactions"]:
        every.update(add_reactions=False)
        if c["write"] != "all":
            for n in c["write"]:
                if n in R:
                    o(R[n]).update(add_reactions=True)

    # особые ограничения ролей
    for n, kw in c["roles"].items():
        if n in R:
            o(R[n]).update(**kw)

    return ow


def resolve_roles(guild):
    R, missing = {}, []
    for name, rid in ROLE_IDS.items():
        role = guild.get_role(rid)
        if role:
            R[name] = role
            print(f"  ✓ {name}: {role.name}")
        else:
            missing.append(f"{name} ({rid})")
    for name, needle in FIND_BY_NAME.items():
        role = next((r for r in guild.roles if needle in r.name.lower()), None)
        if role:
            R[name] = role
            print(f"  ✓ {name}: {role.name} (найдена по названию)")
    return R, missing


async def apply_role_perms(guild, R):
    if FOLL not in R:
        R[FOLL] = await guild.create_role(name="📖 Последователь",
                                          colour=discord.Colour(0xBDC3C7), hoist=True)
        print("  + создана роль 📖 Последователь — поставь её под Посвящённым")
    for name, perms in ROLE_PERMS.items():
        role = R.get(name)
        if not role:
            continue
        try:
            await role.edit(permissions=discord.Permissions(**perms))
            print(f"  ↻ права обновлены: {role.name}")
        except discord.Forbidden:
            print(f"  ! не могу изменить {role.name}: перетащи роль бота выше неё")


def warn_extra_admins(guild, R):
    allowed = {R[FOUNDER].id} if FOUNDER in R else set()
    for role in guild.roles:
        if role.permissions.administrator and not role.managed and role.id not in allowed:
            print(f"  ⚠ у роли «{role.name}» есть «Администратор» — сними вручную, если это не Основатель")


async def create_channel(guild, cat, c, ow):
    kind, name, topic, slow = c["kind"], c["name"], c["topic"], c["slow"]
    if kind == "voice":
        return await guild.create_voice_channel(name, category=cat, overwrites=ow)
    if kind == "forum":
        try:
            return await guild.create_forum(
                name, category=cat, overwrites=ow, topic=topic, slowmode_delay=slow,
                available_tags=[discord.ForumTag(name=t) for t in FORUM_TAGS])
        except discord.HTTPException:
            print(f"    (форум недоступен — {name} создан текстовым)")
    if kind == "news":
        try:
            return await guild.create_text_channel(name, category=cat, overwrites=ow,
                                                   topic=topic, slowmode_delay=slow, news=True)
        except discord.HTTPException:
            print(f"    (для канала объявлений нужен режим «Сообщество» — {name} создан текстовым)")
    return await guild.create_text_channel(name, category=cat, overwrites=ow,
                                           topic=topic, slowmode_delay=slow)


async def setup_channels(guild, R):
    for cat_name, viewers, channels in STRUCTURE:
        cat_ow = category_overwrites(guild, R, viewers)
        cat = discord.utils.get(guild.categories, name=cat_name)
        if cat:
            await cat.edit(overwrites=cat_ow)
            print(f"  ↻ {cat_name}")
        else:
            cat = await guild.create_category(cat_name, overwrites=cat_ow)
            print(f"  + {cat_name}")
        for c in channels:
            ow = channel_overwrites(guild, R, c)
            existing = discord.utils.get(cat.channels, name=c["name"])
            if existing:
                kw = dict(overwrites=ow)
                if c["kind"] != "voice":
                    kw["slowmode_delay"] = c["slow"]
                await existing.edit(**kw)
                print(f"    ↻ {c['name']}")
                continue
            new = await create_channel(guild, cat, c, ow)
            print(f"    + {c['name']}")
            if c["name"] == "📥┃приём-материалов":
                msg = await new.send(INTAKE_TEMPLATE)
                await msg.pin()


class Setup(discord.Client):
    def __init__(self, guild_id):
        super().__init__(intents=discord.Intents.default())
        self.guild_id = guild_id
        self.done = False

    async def on_ready(self):
        if self.done:
            return
        self.done = True
        try:
            guild = self.get_guild(self.guild_id)
            if guild is None:
                print("✖ Бот не найден на сервере с таким ID. Пригласи его и проверь ID.")
                return
            print(f"\nСервер: {guild.name}\n\nРоли…")
            R, missing = resolve_roles(guild)
            if missing:
                print("\n✖ Не найдены роли с ID: " + ", ".join(missing))
                print("  Проверь ID (правый клик по роли → Копировать ID) и запусти снова.")
                return

            print("\nБазовые права @everyone…")
            await guild.default_role.edit(permissions=SAFE_EVERYONE)

            print("Права ролей…")
            await apply_role_perms(guild, R)
            warn_extra_admins(guild, R)

            print("\nКатегории и каналы…")
            await setup_channels(guild, R)

            print("\n✔ Готово. Выгони бота с сервера и сбрось его токен в Developer Portal.")
        except discord.Forbidden:
            print("\n✖ Не хватает прав. Бот должен быть с правом «Администратор», "
                  "а его роль — на самом верху списка ролей.")
        finally:
            await self.close()


def main():
    print("═══ VERITAS · каналы и доступы ═══")
    print("Токен вводится скрыто: после вставки ничего не видно — просто нажми Enter.\n")
    token = getpass.getpass("Токен бота: ").strip()
    guild_id = int(input("ID сервера: ").strip())
    try:
        Setup(guild_id).run(token, log_handler=None)
    except discord.LoginFailure:
        print("✖ Неверный токен. Скопируй заново: Developer Portal → Bot → Reset Token.")
    input("\nНажми Enter, чтобы закрыть…")


if __name__ == "__main__":
    main()
