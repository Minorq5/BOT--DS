"""Запуск API в том же процессе, что и бот."""

from __future__ import annotations

import contextlib
import logging

import uvicorn

log = logging.getLogger("veritas.api")


class EmbeddedServer(uvicorn.Server):
    """uvicorn без перехвата сигналов: остановкой управляет бот."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def make_server(app, host: str, port: int) -> EmbeddedServer:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off",
                            access_log=False, server_header=False)
    return EmbeddedServer(config)
