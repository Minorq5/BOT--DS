"""HTTP API для сайта (FastAPI). Закрыто токеном API_TOKEN (Bearer)."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Request

from .. import __version__
from .models import (
    AnnounceResponse,
    ArchiveAnnouncement,
    ClearanceResponse,
    VaultAnnouncement,
    secret_error,
    url_error,
)


class Bridge(Protocol):
    async def clearance(self, discord_id: int) -> ClearanceResponse: ...
    async def announce_archive(self, item: ArchiveAnnouncement) -> int: ...
    async def announce_vault(self, item: VaultAnnouncement) -> int: ...


class BridgeUnavailable(Exception):
    """Бот не готов или канал не найден."""


class RateLimiter:
    """Скользящее окно запросов на клиента."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        d = self.hits[key]
        while d and now - d[0] >= self.window_s:
            d.popleft()
        if len(d) >= self.limit:
            return False
        d.append(now)
        return True


def create_app(bridge: Bridge | None, token: str | None, allowed_domains: Iterable[str] = (), *,
               rate_limit: int = 60, rate_window_s: float = 60.0,
               clock: Callable[[], float] = time.monotonic,
               status: Callable[[], dict] | None = None) -> FastAPI:
    """Без токена (``token=None``) — только проверка здоровья: нужна хостингам вроде Render,
    которые держат сервис живым, пока на порт приходят запросы."""
    if token is not None and len(token) < 16:
        raise ValueError("API_TOKEN должен быть не короче 16 символов")
    app = FastAPI(title="Veritas API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    def health_body() -> dict:
        body = {"ok": True, "version": __version__}
        if status is not None:
            body.update(status())
        return body

    @app.get("/")
    @app.get("/health")
    @app.head("/")
    @app.head("/health")
    async def root_health() -> dict:
        return health_body()

    if token is None or bridge is None:
        return app

    expected = token.encode("utf-8")
    domains = tuple(allowed_domains)
    limiter = RateLimiter(rate_limit, rate_window_s, clock)

    async def guard(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail="Слишком много запросов")
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode("utf-8"), expected):
            raise HTTPException(status_code=401, detail="Неверный токен",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.get("/api/v1/health")
    async def health() -> dict:
        return health_body()

    @app.get("/api/v1/clearance/{discord_id}", response_model=ClearanceResponse, dependencies=[Depends(guard)])
    async def clearance(discord_id: int) -> ClearanceResponse:
        if discord_id <= 0 or discord_id >= 2**64:
            raise HTTPException(status_code=422, detail="Некорректный Discord ID")
        try:
            return await bridge.clearance(discord_id)
        except BridgeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.post("/api/v1/archive/announce", response_model=AnnounceResponse, dependencies=[Depends(guard)])
    async def archive_announce(item: ArchiveAnnouncement) -> AnnounceResponse:
        error = url_error(item.url, domains) or secret_error(item.title, item.author)
        if error:
            raise HTTPException(status_code=422, detail=error)
        try:
            mid = await bridge.announce_archive(item)
        except BridgeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        return AnnounceResponse(ok=True, message_id=str(mid))

    @app.post("/api/v1/vault/announce", response_model=AnnounceResponse, dependencies=[Depends(guard)])
    async def vault_announce(item: VaultAnnouncement) -> AnnounceResponse:
        error = url_error(item.url, domains) or secret_error(item.title, item.note, item.author)
        if error:
            raise HTTPException(status_code=422, detail=error)
        try:
            mid = await bridge.announce_vault(item)
        except BridgeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        return AnnounceResponse(ok=True, message_id=str(mid))

    return app
