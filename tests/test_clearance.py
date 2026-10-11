import pytest

from veritas import config as C
from veritas.clearance import (
    can_mark,
    can_set_rank,
    can_timeout,
    clearance_level,
    member_rank,
    plan_exile,
    plan_liar,
    plan_restore,
    plan_set_rank,
)

# ───── маппинг допусков

@pytest.mark.parametrize("keys,level", [
    ({C.EXILE}, 0), ({C.STRANGER}, 1), ({C.FOLL}, 2), ({C.INIT}, 3), ({C.TRUST}, 4), ({C.ARCH}, 4),
    ({C.COUNCIL}, 5), ({C.HEAD}, 5), ({C.FOUNDER}, 5), (set(), 0),
    ({C.FOLL, C.TRUST}, 4),                 # максимум по ролям
    ({C.TRUST, C.LIAR}, 2),                 # Лжец ограничивает потолок
    ({C.STRANGER, C.LIAR}, 1),              # но не поднимает
    ({C.COUNCIL, C.EXILE}, 0),              # Отлучённый → 0 всегда
    ({C.LIAR}, 0),
])
def test_clearance_level(keys, level):
    assert clearance_level(keys) == level


# ───── кто что может выдать (таблица §9.1)

def ok(actor, target, new, head=False):
    return can_set_rank({actor} if actor else set(), target, new, head)[0]


@pytest.mark.parametrize("actor,allowed", [
    (C.INIT, False), (C.TRUST, True), (C.ARCH, True), (C.COUNCIL, True), (C.HEAD, True), (C.FOUNDER, True)])
def test_stranger_to_follower(actor, allowed):
    assert ok(actor, {C.STRANGER}, C.FOLL) is allowed


@pytest.mark.parametrize("actor,allowed", [
    (C.TRUST, False), (C.ARCH, True), (C.COUNCIL, True), (C.HEAD, True), (C.FOUNDER, True)])
def test_follower_to_initiate(actor, allowed):
    assert ok(actor, {C.FOLL}, C.INIT) is allowed


@pytest.mark.parametrize("new", [C.TRUST, C.ARCH])
@pytest.mark.parametrize("actor,allowed", [
    (C.TRUST, False), (C.ARCH, False), (C.COUNCIL, True), (C.HEAD, True), (C.FOUNDER, True)])
def test_to_trusted_or_archivist(actor, allowed, new):
    assert ok(actor, {C.INIT}, new) is allowed


def test_to_council_founder_only_unless_flag():
    assert ok(C.FOUNDER, {C.TRUST}, C.COUNCIL)
    assert not ok(C.HEAD, {C.TRUST}, C.COUNCIL)
    assert ok(C.HEAD, {C.TRUST}, C.COUNCIL, head=True)
    assert not ok(C.COUNCIL, {C.TRUST}, C.COUNCIL, head=True)


def test_to_head_founder_only():
    assert ok(C.FOUNDER, {C.COUNCIL}, C.HEAD)
    assert not ok(C.HEAD, {C.COUNCIL}, C.HEAD, head=True)


def test_cannot_touch_equal_or_higher():
    assert not ok(C.TRUST, {C.TRUST}, C.FOLL)          # равный
    assert not ok(C.COUNCIL, {C.HEAD}, C.TRUST)        # выше
    assert not ok(C.ARCH, {C.TRUST}, C.INIT)           # снять Доверенного может только Совет+
    assert ok(C.COUNCIL, {C.TRUST}, C.INIT)


def test_founder_role_never_given():
    assert not ok(C.FOUNDER, {C.HEAD}, C.FOUNDER)


def test_marked_targets_and_actors():
    assert not ok(C.COUNCIL, {C.EXILE}, C.FOLL)
    assert ok(C.COUNCIL, {C.STRANGER, C.LIAR}, C.FOLL)
    assert not ok(C.COUNCIL, {C.FOLL, C.LIAR}, C.INIT)
    assert not can_set_rank({C.TRUST, C.LIAR}, {C.STRANGER}, C.FOLL)[0]


def test_plan_set_rank_is_exclusive():
    remove, add = plan_set_rank({C.FOLL, C.TRUST, C.ARCH}, C.INIT)
    assert remove == {C.FOLL, C.TRUST, C.ARCH} and add == {C.INIT}
    remove, add = plan_set_rank({C.FOUNDER, C.COUNCIL}, C.HEAD)
    assert C.FOUNDER not in remove


# ───── метки

def test_marks_never_on_founder_or_council():
    for protected in (C.FOUNDER, C.HEAD, C.COUNCIL):
        assert not can_mark({C.FOUNDER}, {protected})[0]
    assert can_mark({C.COUNCIL}, {C.TRUST})[0]
    assert not can_mark({C.COUNCIL}, {C.COUNCIL})[0]


def test_plan_liar_keeps_follower():
    remove, add = plan_liar({C.FOLL, C.INIT, C.TRUST})
    assert remove == {C.INIT, C.TRUST} and add == {C.LIAR}
    assert plan_liar({C.STRANGER}) == (set(), {C.LIAR})


def test_plan_exile_removes_all_managed():
    remove, add = plan_exile({C.FOLL, C.INIT, C.LIAR}, C.MANAGED_ROLES)
    assert remove == {C.FOLL, C.INIT, C.LIAR} and add == {C.EXILE}


def test_plan_restore_returns_snapshot_and_drops_marks():
    remove, add = plan_restore({C.FOLL, C.LIAR}, {C.INIT, C.TRUST})
    assert remove == {C.LIAR} and add == {C.INIT, C.TRUST}
    remove, add = plan_restore({C.EXILE}, {C.FOLL, C.LIAR})
    assert remove == {C.EXILE} and add == {C.FOLL}
    remove, add = plan_restore({C.EXILE}, set())
    assert add == {C.STRANGER}  # без ранга человек не остаётся


def test_member_rank():
    assert member_rank({C.FOLL, C.ARCH, C.TRUST}) == C.ARCH
    assert member_rank({C.LIAR}) is None


def test_timeout_limits():
    day, max_ = 24 * 3600, 28 * 24 * 3600
    assert can_timeout({C.TRUST}, {C.FOLL}, day, day, max_)[0]
    assert not can_timeout({C.TRUST}, {C.FOLL}, day + 60, day, max_)[0]
    assert can_timeout({C.COUNCIL}, {C.FOLL}, 7 * day, day, max_)[0]
    assert not can_timeout({C.INIT}, {C.FOLL}, 60, day, max_)[0]
    assert not can_timeout({C.TRUST}, {C.ARCH}, 60, day, max_)[0]
    assert not can_timeout({C.COUNCIL}, {C.FOLL}, max_ + 1, day, max_)[0]
