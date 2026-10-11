"""Единый стиль embed-ов."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import discord

from .config import COLORS

FOOTER = "Veritas · правда не для всех"
MAX_FIELD = 1024
MAX_DESC = 4096


def clip(text: str | None, limit: int = MAX_FIELD) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def make_embed(
    title: str | None = None,
    description: str | None = None,
    *,
    color: str = "brand",
    fields: Iterable[tuple[str, str, bool]] = (),
    footer: str | None = FOOTER,
    timestamp: datetime | None = None,
) -> discord.Embed:
    e = discord.Embed(
        title=clip(title, 256) if title else None,
        description=clip(description, MAX_DESC) if description else None,
        colour=COLORS.get(color, COLORS["brand"]),
        timestamp=timestamp,
    )
    for name, value, inline in fields:
        e.add_field(name=clip(name, 256), value=clip(value) or "—", inline=inline)
    if footer:
        e.set_footer(text=footer)
    return e


def utcnow() -> datetime:
    return datetime.now(UTC)
