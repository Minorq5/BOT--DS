"""Режим хостинга Render: порт, проверка здоровья, само-пинг, копия базы в Discord."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fastapi.testclient import TestClient

from veritas.api.app import create_app
from veritas.cloud_backup import CHANNEL_NAME, FILENAME, DiscordBackup, is_sqlite, snapshot_bytes
from veritas.config import Settings
from veritas.db import Database
from veritas.keepalive import health_url

# ───── настройки

def test_render_env_enables_web_and_keepalive():
    s = Settings.from_env({"PORT": "10000", "RENDER_EXTERNAL_URL": "https://veritas-bot.onrender.com/",
                           "DB_BACKUP_TO_DISCORD": "true"})
    assert s.web_enabled and s.api_host == "0.0.0.0" and s.api_port == 10000
    assert s.public_url == "https://veritas-bot.onrender.com"
    assert s.keepalive_minutes == 10 and s.discord_backup and s.backup_minutes == 10


def test_local_defaults_unchanged():
    s = Settings.from_env({})
    assert not s.web_enabled and s.api_host == "127.0.0.1" and s.api_port == 8080
    assert s.keepalive_minutes == 0 and not s.discord_backup


def test_explicit_values_win():
    s = Settings.from_env({"PORT": "10000", "API_HOST": "127.0.0.1", "API_PORT": "9000",
                           "PUBLIC_URL": "https://x.example", "KEEPALIVE_MINUTES": "5"})
    assert (s.api_host, s.api_port, s.keepalive_minutes) == ("127.0.0.1", 9000, 5)


def test_health_url():
    assert health_url("https://a.onrender.com/") == "https://a.onrender.com/health"


# ───── проверка здоровья без токена

def test_health_only_app():
    c = TestClient(create_app(None, None, status=lambda: {"discord_ready": True}))
    for path in ("/", "/health", "/api/v1/health"):
        r = c.get(path)
        if path == "/api/v1/health":
            assert r.status_code == 404  # API сайта выключено без токена
            continue
        assert r.status_code == 200 and r.json()["discord_ready"] is True
    assert c.head("/").status_code == 200
    assert c.get("/api/v1/clearance/1").status_code == 404


def test_health_available_with_token_too():
    c = TestClient(create_app(MagicMock(), "t" * 32, ("a.com",)))
    assert c.get("/health").status_code == 200
    assert c.get("/api/v1/clearance/1").status_code == 401


# ───── копия базы в Discord

def make_db_file(path, value="1"):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        con.execute("INSERT INTO t VALUES (?)", (value,))
    return path


class FakeAttachment:
    def __init__(self, data):
        self.filename = FILENAME
        self._data = data

    async def read(self):
        return self._data


class FakeBackupChannel:
    def __init__(self):
        self.id = 4242
        self.name = CHANNEL_NAME
        self.type = discord.ChannelType.text
        self.messages = []
        self.edits = 0
        self.send = self._send

    async def _send(self, content=None, file=None, **kw):
        msg = SimpleNamespace(id=len(self.messages) + 1, author=SimpleNamespace(id=999), content=content,
                              attachments=[FakeAttachment(file.fp.read())], created_at=discord.utils.utcnow(),
                              edited_at=None)
        self.messages.append(msg)
        return msg

    def history(self, limit=50):
        async def gen():
            for m in reversed(self.messages):
                yield m
        return gen()

    def get_partial_message(self, mid):
        channel = self

        class Partial:
            async def edit(self, content=None, attachments=()):
                msg = next((m for m in channel.messages if m.id == mid), None)
                if msg is None:
                    raise discord.NotFound(MagicMock(status=404), "gone")
                msg.attachments = [FakeAttachment(attachments[0].fp.read())]
                channel.edits += 1
                return msg
        return Partial()


def fake_bot(tmp_path, channel=None, *, fail=False):
    guild = MagicMock()
    guild.id = 1
    guild.channels = [channel] if channel else []
    guild.default_role = MagicMock()

    async def create_text_channel(name, **kw):
        ch = FakeBackupChannel()
        guild.channels.append(ch)
        return ch

    guild.create_text_channel = AsyncMock(side_effect=create_text_channel)
    if fail:
        guild.fetch_channels = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    bot = SimpleNamespace(settings=Settings(guild_id=1, db_path=str(tmp_path / "data" / "v.db")),
                          user=SimpleNamespace(id=999), get_channel=lambda cid: None)
    bot.get_guild = (lambda gid: None) if fail else (lambda gid: guild)
    bot.fetch_guild = AsyncMock(return_value=guild)
    return bot, guild


def test_snapshot_is_valid_sqlite(tmp_path):
    data = snapshot_bytes(make_db_file(tmp_path / "a.db"))
    assert is_sqlite(data)
    assert not list(tmp_path.glob("tmp*"))


async def test_save_creates_hidden_channel_then_edits_same_message(tmp_path):
    bot, guild = fake_bot(tmp_path)
    (tmp_path / "data").mkdir()
    make_db_file(bot.settings.db_path)
    b = DiscordBackup(bot)
    assert await b.save()
    guild.create_text_channel.assert_awaited_once()
    ow = guild.create_text_channel.await_args.kwargs["overwrites"]
    assert ow[guild.default_role].view_channel is False
    ch = guild.channels[0]
    assert not await b.save()                    # без изменений — не грузим
    make_db_file(bot.settings.db_path, "2")
    assert await b.save()
    assert ch.edits == 1 and len(ch.messages) == 1   # редактируем, а не копим и не удаляем


async def test_restore_downloads_when_local_db_missing(tmp_path):
    ch = FakeBackupChannel()
    src = make_db_file(tmp_path / "src.db", "сохранено")
    await ch._send("copy", file=discord.File(str(src), filename=FILENAME))
    bot, _ = fake_bot(tmp_path, ch)
    b = DiscordBackup(bot)
    text = await b.restore_if_missing()
    assert "восстановлена" in text
    db = Database(bot.settings.db_path)
    await db.connect()
    assert await db.fetchval("SELECT v FROM t") == "сохранено"
    await db.close()
    assert b.enabled and b.message_id == 1


async def test_restore_skipped_when_local_exists(tmp_path):
    bot, _ = fake_bot(tmp_path)
    (tmp_path / "data").mkdir()
    make_db_file(bot.settings.db_path)
    assert "на месте" in await DiscordBackup(bot).restore_if_missing()


async def test_no_backup_yet_starts_fresh(tmp_path):
    bot, _ = fake_bot(tmp_path)
    b = DiscordBackup(bot)
    assert "ещё нет" in await b.restore_if_missing()
    assert b.enabled


async def test_restore_error_disables_saving(tmp_path):
    bot, _ = fake_bot(tmp_path, fail=True)
    b = DiscordBackup(bot)
    assert "выключено" in await b.restore_if_missing()
    assert not b.enabled
    (tmp_path / "data").mkdir()
    make_db_file(bot.settings.db_path)
    assert not await b.save()   # пустая база не затрёт хорошую копию


@pytest.mark.parametrize("data", [b"", b"not a database"])
async def test_corrupt_backup_disables_saving(tmp_path, data):
    ch = FakeBackupChannel()
    ch.messages.append(SimpleNamespace(id=1, author=SimpleNamespace(id=999),
                                       attachments=[FakeAttachment(data)], created_at=None, edited_at=None))
    bot, _ = fake_bot(tmp_path, ch)
    b = DiscordBackup(bot)
    assert "повреждена" in await b.restore_if_missing()
    assert not b.enabled
