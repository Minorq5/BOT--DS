"""Симулятор прав: 9 персонажей × 16 каналов. Эталонная матрица зафиксирована здесь.

— не видит, R читает, W пишет (форум: создаёт публикации; голос: подключается и говорит).
"""

import pytest

from veritas import config as C
from veritas.permissions import (
    BOT,
    EVERYONE,
    HIDDEN,
    READ,
    WRITE,
    access,
    can,
    category_overwrites,
    compute_overwrites,
    effective_permissions,
    ticket_overwrites,
)

PERSONAS = {
    "Незнакомец": {C.STRANGER},
    "Последователь": {C.FOLL},
    "Лжец": {C.FOLL, C.LIAR},
    "Посвящённый": {C.INIT},
    "Доверенный": {C.TRUST},
    "Архивариус": {C.ARCH},
    "Член Совета": {C.COUNCIL},
    "Глава": {C.HEAD},
    "Отлучённый": {C.EXILE},
}

COLUMNS = ["rules", "verify", "news", "general", "voice", "requests", "archive", "intake",
           "investigations", "applications", "secrets", "vault", "council", "court", "logs", "appeals"]

#                rules verify news gen voice req  arch intake inv  appl secr vault counc court logs appeal
EXPECTED = {
    "Незнакомец":    "R R — — — — — — — — — — — — — —",
    "Последователь": "R — R W W R — — — — — — — — — —",
    "Лжец":          "R — R W W R — — — — — — — — — —",
    "Посвящённый":   "R — R W W R R R R — — — — — — —",
    "Доверенный":    "R R R W W R R R R R W R — — — —",
    "Архивариус":    "R — R W W R W R W R W W — — — —",
    "Член Совета":   "R R R W W R R R W R W W W W R W",
    "Глава":         "W W W W W W W W W R W W W W R W",
    "Отлучённый":    "— — — — — — — — — — — — — — — W",
}


def test_columns_cover_all_sixteen_channels():
    assert COLUMNS == [c.key for _, c in C.all_channels()]


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_matrix_row(persona):
    expected = EXPECTED[persona].split()
    actual = [access(PERSONAS[persona], C.channel_spec(k)) for k in COLUMNS]
    assert dict(zip(COLUMNS, actual, strict=True)) == dict(zip(COLUMNS, expected, strict=True))


def visible(persona):
    return {k for k in COLUMNS if access(PERSONAS[persona], C.channel_spec(k)) != HIDDEN}


def test_stranger_sees_only_rules_and_verification():
    assert visible("Незнакомец") == {"rules", "verify"}


def test_exile_sees_only_appeals():
    assert visible("Отлучённый") == {"appeals"}


def test_liar_has_no_files_links_threads_or_stream():
    liar = PERSONAS["Лжец"]
    general, voice = C.channel_spec("general"), C.channel_spec("voice")
    assert not can(liar, general, "attach_files")
    assert not can(liar, general, "embed_links")
    assert not can(liar, general, "create_public_threads")
    assert can(liar, general, "send_messages")
    assert not can(liar, voice, "stream")
    # у обычного Последователя всё это есть
    assert can({C.FOLL}, general, "attach_files")
    assert can({C.FOLL}, voice, "stream")


def test_council_cannot_write_rules_news_requests():
    for key in ("rules", "news", "requests"):
        assert access({C.COUNCIL}, C.channel_spec(key)) == READ


def test_verification_hidden_from_passed_but_visible_to_reviewers():
    v = C.channel_spec("verify")
    for keys in ({C.FOLL}, {C.INIT}, {C.ARCH}):
        assert access(keys, v) == HIDDEN
    # Доверенный, у которого есть и Последователь, всё равно видит (разрешение побеждает)
    assert access({C.FOLL, C.TRUST}, v) == READ


def test_only_bot_writes_logs_and_applications():
    for key in ("logs", "applications"):
        c = C.channel_spec(key)
        for name, keys in PERSONAS.items():
            assert access(keys, c) != WRITE, name
        ow = compute_overwrites()[key]
        assert ow[BOT]["send_messages"] is True


def test_forum_replies_allowed_for_all_viewers():
    forum = C.channel_spec("investigations")
    assert access({C.INIT}, forum) == READ
    assert can({C.INIT}, forum, "send_messages_in_threads")


def test_founder_admin_sees_everything():
    for _, c in C.all_channels():
        assert access({C.FOUNDER}, c) == WRITE


def test_slowmode_preserved():
    assert C.channel_spec("appeals").slow == 600


def test_intake_is_panel_only():
    """#приём-материалов — только кнопка: писать там могут лишь Основатель и Глава."""
    intake = C.channel_spec("intake")
    for keys in ({C.INIT}, {C.TRUST}, {C.ARCH}, {C.COUNCIL}):
        assert access(keys, intake) == READ
        assert not can(keys, intake, "create_public_threads")
    assert access({C.HEAD}, intake) == WRITE


def test_category_overwrites():
    entry = next(c for c in C.STRUCTURE if c.key == "cat_entry")
    ow = category_overwrites(entry)
    assert ow[EVERYONE] == {"view_channel": True}
    assert ow[C.EXILE] == {"view_channel": False}
    tickets = next(c for c in C.STRUCTURE if c.key == "cat_tickets")
    ow = category_overwrites(tickets)
    assert ow[EVERYONE] == {"view_channel": False}
    assert set(k for k, v in ow.items() if v.get("view_channel")) == {C.FOUNDER, C.HEAD, C.COUNCIL, BOT}
    assert ow[BOT]["manage_channels"] is True


def test_every_managed_channel_has_bot_overwrite():
    for key, ow in compute_overwrites().items():
        assert ow[BOT]["view_channel"] is True, key


def test_ticket_overwrites_access_vs_council_types():
    ow = ticket_overwrites([C.TRUST, C.ARCH, C.COUNCIL, C.HEAD, C.FOUNDER])
    assert effective_permissions({C.STRANGER}, ow) == set()
    author = effective_permissions({C.FOLL}, ow, member_target="@author")
    assert {"view_channel", "send_messages"} <= author
    assert "send_messages" in effective_permissions({C.TRUST}, ow)
    council_only = ticket_overwrites([C.COUNCIL, C.HEAD, C.FOUNDER])
    assert effective_permissions({C.TRUST}, council_only) == set()


def test_admin_returns_none():
    assert effective_permissions({C.FOUNDER}, {}) is None
