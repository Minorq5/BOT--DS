"""Схемы и валидация запросов API сайта."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..logic.raid import host_allowed

# Ключи и пароли через бота не передаются никогда — грубая защита от случайной вставки.
_SECRET_RE = re.compile(r"(парол|password|passwd|secret|ключ\s*[:=]|key\s*[:=]|token\s*[:=])", re.I)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ArchiveAnnouncement(_Base):
    number: int = Field(ge=1, le=999_999, description="Номер досье: 42 → «ДОСЬЕ-0042»")
    title: str = Field(min_length=1, max_length=200)
    level: int = Field(ge=2, le=5, description="Гриф: минимальный допуск 2–5")
    url: str = Field(min_length=8, max_length=500)
    reliability: str = Field(min_length=1, max_length=60, description="Достоверность")
    author: str = Field(min_length=1, max_length=100)


class VaultAnnouncement(_Base):
    title: str = Field(min_length=1, max_length=200)
    level: int = Field(ge=4, le=5)
    url: str = Field(min_length=8, max_length=500)
    author: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class ClearanceResponse(BaseModel):
    discord_id: str
    member: bool
    level: int
    roles: list[str]
    is_liar: bool
    is_exiled: bool


class AnnounceResponse(BaseModel):
    ok: bool
    message_id: str


def url_error(url: str, allowed_domains: Iterable[str]) -> str | None:
    """None — ссылка годится; иначе текст ошибки."""
    allowed = [d for d in allowed_domains if d]
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Некорректная ссылка."
    if parts.scheme != "https":
        return "Ссылка должна начинаться с https://"
    if not parts.hostname:
        return "В ссылке нет домена."
    if parts.username or parts.password:
        return "Ссылка не должна содержать логин или пароль."
    if not allowed:
        return "ALLOWED_SITE_DOMAINS не задан — ссылки запрещены."
    if not host_allowed(parts.hostname, allowed):
        return f"Домен {parts.hostname} не в списке разрешённых."
    return None


def secret_error(*texts: str | None) -> str | None:
    for t in texts:
        if t and _SECRET_RE.search(t):
            return "Похоже на ключ или пароль. Через бота они не передаются."
    return None


def dossier_code(number: int) -> str:
    return f"ДОСЬЕ-{number:04d}"
