"""Точка входа.

    python -m veritas                 — запустить бота
    python -m veritas setup [--dry-run] — настроить сервер и выйти
    python -m veritas backup          — резервная копия базы в data/backups/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import aiohttp
import discord

from .config import Settings, load_dotenv
from .db import backup

log = logging.getLogger("veritas")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)


async def run_bot(settings: Settings) -> int:
    from .bot import VeritasBot

    for message_content in (True, False):
        bot = VeritasBot(settings, message_content=message_content)
        try:
            async with bot:
                await bot.start(settings.discord_token)
            return 0
        except discord.PrivilegedIntentsRequired:
            if message_content:
                log.warning("Discord отклонил привилегированные intents. Пробую без Message Content "
                            "(функции, которым нужен текст сообщений, будут отключены).")
                continue
            log.error("Включи «Server Members Intent» в Developer Portal → Bot → Privileged Gateway "
                      "Intents. Без него бот не может работать.")
            return 1
        except discord.LoginFailure:
            log.error("Неверный токен. Developer Portal → Bot → Reset Token, затем обнови .env.")
            return 1
        except (aiohttp.ClientError, OSError) as exc:
            log.error("Нет связи с Discord: %s", exc)
            return 1
    return 1


async def run_setup(settings: Settings, dry_run: bool) -> int:
    from .setup_runner import run_setup_cli

    return await run_setup_cli(settings, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m veritas", description="Бот сервера Veritas")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="запустить бота (по умолчанию)")
    p_setup = sub.add_parser("setup", help="настроить роли, права и каналы")
    p_setup.add_argument("--dry-run", action="store_true", help="только показать план, ничего не менять")
    sub.add_parser("backup", help="резервная копия базы")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if args.command == "backup":
        try:
            path = backup(settings.db_path)
        except FileNotFoundError as exc:
            print(f"✖ {exc}")
            return 1
        print(f"✔ Резервная копия: {path}")
        return 0

    if not settings.discord_token:
        print("✖ Нет DISCORD_TOKEN. Скопируй .env.example в .env и заполни.")
        return 1
    if not settings.guild_id:
        print("✖ Нет GUILD_ID. Включи режим разработчика в Discord, ПКМ по серверу → Копировать ID.")
        return 1

    if args.command == "setup":
        return asyncio.run(run_setup(settings, args.dry_run))
    return asyncio.run(run_bot(settings))


if __name__ == "__main__":
    sys.exit(main())
