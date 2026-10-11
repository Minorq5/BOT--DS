"""Само-пинг: держит бесплатный веб-сервис Render «бодрствующим».

Render усыпляет бесплатный сервис через 15 минут без входящих HTTP-запросов. Бот раз в
``KEEPALIVE_MINUTES`` минут сам запрашивает свой публичный адрес (``RENDER_EXTERNAL_URL``) —
запрос проходит через внешний вход Render и считается входящим трафиком.
Для надёжности добавь ещё внешний пинг (UptimeRobot и т. п.) — см. docs/RENDER.md.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("veritas.keepalive")


def health_url(public_url: str) -> str:
    return public_url.rstrip("/") + "/health"


async def keepalive_loop(public_url: str, minutes: int, *, first_delay: float = 60.0) -> None:
    url = health_url(public_url)
    timeout = aiohttp.ClientTimeout(total=30)
    await asyncio.sleep(first_delay)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(url) as resp:
                    log.debug("keepalive %s → %s", url, resp.status)
                    if resp.status >= 400:
                        log.warning("Само-пинг %s вернул %s", url, resp.status)
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning("Само-пинг %s не прошёл: %s", url, exc)
            await asyncio.sleep(minutes * 60)
