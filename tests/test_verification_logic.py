import pytest

from veritas.config import VerificationConfig
from veritas.logic.verification import (
    ACCEPTED,
    PENDING,
    REJECTED,
    SUBMITTED,
    AlreadyDecided,
    Answers,
    Attempt,
    agrees,
    check_submission,
    human_duration,
    is_junk,
    next_status,
    validate_answers,
)

CFG = VerificationConfig()
NOW = 1_000_000.0
GOOD = Answers("Вейл", "от друга из игры", "Хочу помогать архиву и узнавать правду о нашем мире.", "да")


def decide(attempts=(), pending=False, age=30.0, now=NOW):
    return check_submission(now, attempts, has_pending=pending, account_age_days=age, cfg=CFG)


def test_first_submission_allowed():
    d = decide()
    assert d.allowed and not d.manual and d.flags == []


def test_pending_blocks():
    d = decide(pending=True)
    assert not d.allowed and "уже" in d.reason


def test_resubmit_cooldown_boundary():
    a = [Attempt(NOW - CFG.resubmit_cooldown_s + 1, SUBMITTED)]
    assert not decide(a).allowed
    a = [Attempt(NOW - CFG.resubmit_cooldown_s, SUBMITTED)]
    assert decide(a).allowed


def test_reject_cooldown_24h_boundary():
    a = [Attempt(NOW - 3600, SUBMITTED), Attempt(NOW - 3600, REJECTED)]
    d = decide(a)
    assert not d.allowed and "через" in d.reason and d.retry_after == pytest.approx(23 * 3600)
    a = [Attempt(NOW - CFG.reject_cooldown_s, REJECTED)]
    assert decide(a).allowed


def test_max_three_attempts_per_day():
    a = [Attempt(NOW - 20 * 3600, SUBMITTED), Attempt(NOW - 10 * 3600, SUBMITTED),
         Attempt(NOW - 3 * 3600, SUBMITTED)]
    d = decide(a)
    assert not d.allowed and "3 попыток" in d.reason
    # самая старая выпала из суток → снова можно
    a[0] = Attempt(NOW - 25 * 3600, SUBMITTED)
    assert decide(a).allowed


def test_young_account_goes_to_manual_with_flag():
    d = decide(age=1.0)
    assert d.allowed and d.manual
    assert d.flags and d.flags[0].startswith("⚠️")
    assert not decide(age=3.0).manual


def test_validate_answers():
    assert validate_answers(GOOD, CFG) == []
    short = Answers("Вейл", "друг", "хочу", "да")
    assert any("минимум 30" in e for e in validate_answers(short, CFG))
    junk = Answers("Вейл", "друг", "аааааааааааааааааааааааааааааааааааа", "да")
    assert validate_answers(junk, CFG)
    no = Answers("Вейл", "друг", GOOD.why, "нет")
    assert any("согласиться" in e for e in validate_answers(no, CFG))


@pytest.mark.parametrize("text,ok", [("да", True), ("Да!", True), ("ДА.", True), ("да, согласен", True),
                                     ("нет", False), ("", False), ("ok", False)])
def test_agrees(text, ok):
    assert agrees(text) is ok


@pytest.mark.parametrize("text,junk", [
    ("Хочу узнать правду и помогать архиву", False),
    ("12345678901234567890123456789012", True),
    ("фывапролджфывапролджфывапролджфыва", True),
    ("ыыыыыыыыыыы", True),
    ("", True),
])
def test_is_junk(text, junk):
    assert is_junk(text) is junk


def test_state_transitions():
    assert next_status(PENDING, "accept") == ACCEPTED
    assert next_status(PENDING, "reject") == REJECTED
    for done in (ACCEPTED, REJECTED):
        with pytest.raises(AlreadyDecided):
            next_status(done, "accept")
    with pytest.raises(ValueError):
        next_status(PENDING, "maybe")


def test_human_duration():
    assert human_duration(24 * 3600) == "1 дн"
    assert human_duration(10 * 60) == "10 мин"
    assert human_duration(3900) == "1 ч 5 мин"
    assert human_duration(5) == "меньше минуты"
