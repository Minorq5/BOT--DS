"""SQLite-хранилище бота (aiosqlite) и простые миграции.

Миграции — файлы ``veritas/migrations/NNN_*.sql``; применённая версия хранится
в таблице ``schema_version``. Все запросы параметризованные.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found = []
    for p in directory.iterdir():
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ───────────── жизненный цикл ─────────────

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.migrate()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def migrate(self) -> int:
        assert self.conn is not None
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        cur = await self.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] or 0
        for version, path in list_migrations():
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            await self.conn.executescript(sql)
            await self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            await self.conn.commit()
            current = version
        return current

    # ───────────── базовые операции ─────────────

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Выполнить запрос с коммитом; вернуть rowcount (или lastrowid для INSERT)."""
        assert self.conn is not None
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return cur.lastrowid
            return cur.rowcount

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self.conn is not None
        cur = await self.conn.execute(sql, tuple(params))
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None
        cur = await self.conn.execute(sql, tuple(params))
        return list(await cur.fetchall())

    async def fetchval(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # ───────────── настройки (key/value) ─────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return await self.fetchval("SELECT value FROM settings WHERE key = ?", (key,), default)

    async def set_setting(self, key: str, value: str | None) -> None:
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        rows = await self.fetchall("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))
        return {r["key"][len(prefix):]: r["value"] for r in rows}

    async def get_state(self, key: str, default: Any = None) -> Any:
        raw = await self.fetchval("SELECT value FROM raid_state WHERE key = ?", (key,))
        if raw is None:
            return default
        return json.loads(raw)

    async def set_state(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO raid_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ───────────── управляемые каналы и сообщения ─────────────

    async def managed_channels(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT key, channel_id FROM managed_channels")
        return {r["key"]: r["channel_id"] for r in rows}

    async def save_managed_channel(self, key: str, channel_id: int, category_id: int | None) -> None:
        await self.execute(
            "INSERT INTO managed_channels (key, channel_id, category_id) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET channel_id = excluded.channel_id, "
            "category_id = excluded.category_id",
            (key, channel_id, category_id),
        )

    async def managed_message(self, key: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT channel_id, message_id FROM managed_messages WHERE key = ?", (key,))

    async def save_managed_message(self, key: str, channel_id: int, message_id: int) -> None:
        await self.execute(
            "INSERT INTO managed_messages (key, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET channel_id = excluded.channel_id, "
            "message_id = excluded.message_id",
            (key, channel_id, message_id),
        )

    # ───────────── метки ─────────────

    async def is_exiled(self, user_id: int) -> bool:
        return await self.fetchone("SELECT 1 FROM exiled WHERE user_id = ?", (user_id,)) is not None

    async def is_liar(self, user_id: int) -> bool:
        return await self.fetchone("SELECT 1 FROM liars WHERE user_id = ?", (user_id,)) is not None

    # ───────────── логи ─────────────

    async def log_settings(self) -> dict[str, bool]:
        rows = await self.fetchall("SELECT category, enabled FROM log_settings")
        return {r["category"]: bool(r["enabled"]) for r in rows}

    async def set_log_setting(self, category: str, enabled: bool) -> None:
        await self.execute(
            "INSERT INTO log_settings (category, enabled) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET enabled = excluded.enabled",
            (category, int(enabled)),
        )


def backup(db_path: str | Path, backups_dir: str | Path | None = None,
           now: datetime | None = None) -> Path:
    """Скопировать SQLite-файл в data/backups/ с датой (безопасно даже на работающей БД)."""
    src = Path(db_path)
    if not src.is_file():
        raise FileNotFoundError(f"База не найдена: {src}")
    target_dir = Path(backups_dir) if backups_dir else src.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"{src.stem}-{stamp}{src.suffix}"
    try:
        with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dst)) as d:
            s.backup(d)
    except sqlite3.Error:
        shutil.copy2(src, dst)
    return dst


def now_ts() -> float:
    return time.time()
