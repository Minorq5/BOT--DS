from datetime import UTC, datetime

import pytest

from veritas.db import Database, backup, list_migrations


def test_migrations_are_numbered():
    versions = [v for v, _ in list_migrations()]
    assert versions == sorted(versions)
    assert versions[0] == 1


async def test_migrate_is_idempotent(db):
    v1 = await db.fetchval("SELECT MAX(version) FROM schema_version")
    again = await db.migrate()
    assert again == v1
    rows = await db.fetchall("SELECT version FROM schema_version")
    assert len(rows) == len(list_migrations())


async def test_tables_exist(db):
    names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("managed_channels", "managed_messages", "applications", "verification_attempts", "tickets",
              "role_snapshots", "sanctions", "exiled", "activity", "log_settings", "raid_state"):
        assert t in names


async def test_settings_and_state(db):
    await db.set_setting("role_id.FOLL", "42")
    await db.set_setting("role_id.FOLL", "43")
    assert await db.get_setting("role_id.FOLL") == "43"
    assert await db.settings_with_prefix("role_id.") == {"FOLL": "43"}
    await db.set_state("lockdown", {"active": True, "ids": [1, 2]})
    assert await db.get_state("lockdown") == {"active": True, "ids": [1, 2]}
    assert await db.get_state("missing", 5) == 5


async def test_managed_ids(db):
    await db.save_managed_channel("rules", 1, 10)
    await db.save_managed_channel("rules", 2, 10)
    assert await db.managed_channels() == {"rules": 2}
    await db.save_managed_message("rules", 2, 99)
    row = await db.managed_message("rules")
    assert (row["channel_id"], row["message_id"]) == (2, 99)


async def test_backup_copies_file(tmp_path):
    database = Database(tmp_path / "v.db")
    await database.connect()
    await database.set_setting("x", "1")
    await database.close()
    dst = backup(tmp_path / "v.db", now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert dst.name == "v-20260102-030405.db"
    assert dst.parent.name == "backups"
    copy = Database(dst)
    await copy.connect()
    assert await copy.get_setting("x") == "1"
    await copy.close()


def test_backup_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "nope.db")
