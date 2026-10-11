"""Очередь логов в #логи: батчинг, ретраи при 429/5xx, переключатели категорий.

Логи не логируют сами себя: ошибки доставки пишутся только в консоль.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from . import config
from .logic import logs as fmt

if TYPE_CHECKING:
    from .bot import VeritasBot

log = logging.getLogger("veritas.logs")

MAX_EMBEDS_PER_MESSAGE = 10
BATCH_WAIT_S = 1.0
MAX_RETRIES = 5


@dataclass
class LogItem:
    embed: discord.Embed
    file_name: str | None = None
    file_data: bytes | None = None
    content: str | None = None


class LogService:
    def __init__(self, bot: VeritasBot | None = None, *, channel_getter: Callable[[], object] | None = None,
                 maxsize: int = 5000, sleep=asyncio.sleep):
        self.bot = bot
        self._channel_getter = channel_getter
        self.queue: asyncio.Queue[LogItem] = asyncio.Queue(maxsize=maxsize)
        self.enabled: dict[str, bool] = dict(config.LOG_CATEGORIES)
        self.dropped = 0
        self.sent = 0
        self._task: asyncio.Task | None = None
        self._carry: LogItem | None = None
        self._sleep = sleep

    # ── настройки

    async def load_settings(self) -> None:
        if self.bot is not None:
            self.enabled.update(await self.bot.db.log_settings())

    async def set_enabled(self, category: str, value: bool) -> None:
        if category not in config.LOG_CATEGORIES:
            raise KeyError(category)
        self.enabled[category] = value
        if self.bot is not None:
            await self.bot.db.set_log_setting(category, value)

    def is_enabled(self, category: str) -> bool:
        return self.enabled.get(category, True)

    # ── постановка в очередь

    def send(self, category: str, embed: discord.Embed, *, content: str | None = None, force: bool = False) -> None:
        if not force and not self.is_enabled(category):
            return
        self._put(LogItem(embed, content=content))

    def send_file(self, embed: discord.Embed, name: str, data: bytes) -> None:
        self._put(LogItem(embed, file_name=name, file_data=data))

    def _put(self, item: LogItem) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("Очередь логов переполнена, событие пропущено (всего %d)", self.dropped)

    def bot_event(self, title: str, description: str, *, color: str = "blue", category: str = "bot") -> None:
        self.send(category, fmt.info(title, description, color))

    def warn_event(self, title: str, description: str) -> None:
        self.send("bot", fmt.info(f"⚠️ {title}", description, "yellow"), force=True)

    def error_event(self, where: str, exc: BaseException, user: discord.abc.User | None = None) -> None:
        who = f" · {user.mention}" if user is not None else ""
        self.send("bot", fmt.info("❗ Ошибка", f"{where}{who}: `{type(exc).__name__}`", "red"), force=True)

    # ── доставка

    def _channel(self):
        if self._channel_getter is not None:
            return self._channel_getter()
        gm = self.bot.guild_map() if self.bot is not None else None
        return gm.channel("logs") if gm else None

    async def start(self) -> None:
        await self.load_settings()
        if self._task is None:
            self._task = asyncio.create_task(self._worker(), name="veritas-logs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _worker(self) -> None:
        if self.bot is not None:
            await self.bot.wait_until_ready()
        while True:
            batch = await self.next_batch()
            try:
                await self.deliver(batch)
            except Exception:  # noqa: BLE001 — воркер не должен умирать
                log.exception("Не удалось доставить логи")

    async def next_batch(self, wait: float = BATCH_WAIT_S) -> list[LogItem]:
        if self._carry is not None:
            first, self._carry = self._carry, None
        else:
            first = await self.queue.get()
        batch = [first]
        if first.file_data is not None:
            return batch
        deadline = time.monotonic() + wait
        while len(batch) < MAX_EMBEDS_PER_MESSAGE:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                break
            if item.file_data is not None:
                self._carry = item  # файл уходит отдельным сообщением следующим заходом
                break
            batch.append(item)
        return batch

    async def deliver(self, batch: list[LogItem]) -> bool:
        channel = self._channel()
        if channel is None:
            log.info("Канал #логи не найден, %d событий только в консоли: %s", len(batch),
                     "; ".join(i.embed.title or "" for i in batch))
            return False
        kwargs: dict = {"embeds": [i.embed for i in batch]}
        content = "\n".join(i.content for i in batch if i.content)
        if content:
            kwargs["content"] = content[:2000]
        delay = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if batch[0].file_data is not None:
                    file = discord.File(io.BytesIO(batch[0].file_data), filename=batch[0].file_name)
                    await channel.send(embed=batch[0].embed, file=file)
                else:
                    await channel.send(**kwargs, allowed_mentions=discord.AllowedMentions(roles=True))
                self.sent += len(batch)
                return True
            except discord.HTTPException as exc:
                retryable = exc.status == 429 or exc.status >= 500
                if not retryable or attempt == MAX_RETRIES:
                    log.error("Логи не доставлены (%s): %s", exc.status, exc.text)
                    return False
                retry_after = getattr(exc, "retry_after", None) or delay
                await self._sleep(retry_after)
                delay = min(delay * 2, 30)
        return False
