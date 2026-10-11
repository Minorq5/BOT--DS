from veritas import config as C
from veritas.config import Settings
from veritas.setup_runner import SetupRunner

from .fakes import FakeGuild


async def test_dry_run_changes_nothing(db):
    g = FakeGuild(with_follower=False)
    report = await SetupRunner(g, db, Settings(), dry_run=True).run()
    text = report.text()
    assert not report.aborted
    assert "+ создать роль 📖 Последователь" in text
    assert "+ 🚪 ВХОД" in text and "+ 📜┃правила" in text and "+ 🎫 ТИКЕТЫ" in text
    assert "Сообщество" in text  # предупреждение про канал объявлений
    g.create_role.assert_not_called()
    g.create_category.assert_not_called()
    g.create_text_channel.assert_not_called()
    assert await db.managed_channels() == {}


async def test_missing_role_ids_abort(db):
    g = FakeGuild()
    g.roles = [r for r in g.roles if r.id != C.ROLE_IDS[C.HEAD]]
    report = await SetupRunner(g, db, Settings(), dry_run=True).run()
    assert report.aborted
    assert "Не найдены роли" in report.text()


async def test_run_creates_everything_then_is_idempotent(db, monkeypatch):
    monkeypatch.setattr("veritas.setup_runner.STEP_PAUSE_S", 0)
    g = FakeGuild(with_follower=False, community=True)
    report = await SetupRunner(g, db, Settings(), dry_run=False).run()
    assert report.errors == [], report.text()
    stored = await db.managed_channels()
    assert len(stored) == 16 + len(C.STRUCTURE)
    assert g.create_role.await_count == 1
    assert await db.get_setting("role_id.FOLL") is not None
    assert g.create_forum.await_count == 1
    assert g.create_voice_channel.await_count == 1
    news = g.get_channel(stored["news"])
    assert news.is_news()
    rules_msg = await db.managed_message("rules")
    assert rules_msg is not None and await db.managed_message("intake") is not None
    intake = g.get_channel(stored["intake"])
    sent = intake.messages[(await db.managed_message("intake"))["message_id"]]
    assert sent.pinned and "Как сдать материал" in sent.content

    # повторный запуск: ничего не создаётся, сообщения редактируются, а не дублируются
    calls_before = (g.create_category.await_count, g.create_text_channel.await_count)
    report2 = await SetupRunner(g, db, Settings(), dry_run=False).run()
    assert report2.errors == []
    assert (g.create_category.await_count, g.create_text_channel.await_count) == calls_before
    assert g.create_role.await_count == 1
    assert len(intake.messages) == 1
    assert "↻ текст «intake» обновлён" in report2.text()
    assert "↻ 📜┃правила: права" not in report2.text()
    assert await db.managed_channels() == stored
