from veritas import config as C
from veritas.logic.roles import plan_role_positions


def test_reorders_only_within_own_slots_below_bot():
    current = {C.STRANGER: 9, C.FOLL: 2, C.INIT: 8, C.TRUST: 7, C.LIAR: 3, C.EXILE: 1}
    changes, problems = plan_role_positions(C.ROLE_ORDER, current, bot_top=20)
    final = {**current, **changes}
    order = [k for k in C.ROLE_ORDER if k in final]
    assert [final[k] for k in order] == sorted(final.values(), reverse=True)
    assert set(final.values()) == set(current.values())
    assert problems == []


def test_roles_above_bot_reported_not_moved():
    current = {C.FOUNDER: 30, C.COUNCIL: 29, C.HEAD: 28, C.ARCH: 10, C.TRUST: 11}
    changes, problems = plan_role_positions(C.ROLE_ORDER, current, bot_top=20)
    assert C.HEAD not in changes and C.COUNCIL not in changes
    assert (C.HEAD, C.COUNCIL) in problems
    assert changes == {C.ARCH: 11, C.TRUST: 10}


def test_already_ordered_no_changes():
    current = {k: 100 - i for i, k in enumerate(C.ROLE_ORDER)}
    assert plan_role_positions(C.ROLE_ORDER, current, bot_top=50) == ({}, [])
