"""Правила верификации: анти-абьюз, проверка анкеты, переходы состояний. Без Discord."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..config import VerificationConfig

DAY = 24 * 3600

PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"
SUBMITTED = "submitted"


def human_duration(seconds: float) -> str:
    """«24 ч», «10 мин», «1 ч 5 мин», «меньше минуты»."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "меньше минуты"
    days, rest = divmod(seconds, DAY)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "меньше минуты"


# ───────────────────── АНКЕТА ─────────────────────


@dataclass(frozen=True)
class Answers:
    name: str
    source: str
    why: str
    agree: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source, "why": self.why, "agree": self.agree}


_AGREE = {"да", "yes", "согласен", "согласна", "да, согласен", "да, согласна"}


def agrees(text: str) -> bool:
    t = re.sub(r"[\s!.,]+$", "", (text or "").strip().lower())
    return t in _AGREE or t.startswith("да")


def is_junk(text: str) -> bool:
    """Мусор: почти нет букв, одна буква по кругу, клавиатурная каша без пробелов."""
    t = (text or "").strip()
    if not t:
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) < max(2, len(t) * 0.5):
        return True
    if len(set(c.lower() for c in letters)) <= 2 and len(letters) > 4:
        return True
    longest_word = max((len(w) for w in t.split()), default=0)
    if len(t) >= 20 and longest_word >= 25:
        return True
    return bool(re.search(r"(.)\1{6,}", t))


def validate_answers(a: Answers, cfg: VerificationConfig) -> list[str]:
    errors = []
    if len(a.name.strip()) < 2 or re.search(r"(.)\1{6,}", a.name):
        errors.append("Укажи ник или имя персонажа.")
    if len(a.source.strip()) < 2 or is_junk(a.source):
        errors.append("Напиши, откуда ты узнал о Veritas.")
    if len(a.why.strip()) < cfg.min_reason_len:
        errors.append(f"Ответ «почему хочешь вступить» — минимум {cfg.min_reason_len} символов.")
    elif is_junk(a.why):
        errors.append("Ответ «почему хочешь вступить» похож на набор символов. Ответь своими словами.")
    if not agrees(a.agree):
        errors.append("Чтобы войти, нужно согласиться с правилами (напиши «да»).")
    return errors


# ───────────────────── АНТИ-АБЬЮЗ ─────────────────────


@dataclass(frozen=True)
class Attempt:
    ts: float
    result: str  # submitted / rejected / accepted


@dataclass
class SubmitDecision:
    allowed: bool
    reason: str = ""
    retry_after: float = 0.0
    manual: bool = False
    flags: list[str] = field(default_factory=list)


def check_submission(now: float, attempts: Iterable[Attempt], *, has_pending: bool,
                     account_age_days: float | None, cfg: VerificationConfig) -> SubmitDecision:
    attempts = sorted(attempts, key=lambda a: a.ts)
    if has_pending:
        return SubmitDecision(False, "Твоя анкета уже у Доверенных. Дождись решения.")
    rejections = [a.ts for a in attempts if a.result == REJECTED]
    if rejections:
        wait = rejections[-1] + cfg.reject_cooldown_s - now
        if wait > 0:
            return SubmitDecision(False, f"После отказа подать анкету снова можно через {human_duration(wait)}.",
                                  retry_after=wait)
    submissions = [a.ts for a in attempts if a.result == SUBMITTED]
    if submissions:
        wait = submissions[-1] + cfg.resubmit_cooldown_s - now
        if wait > 0:
            return SubmitDecision(False, f"Слишком часто. Попробуй через {human_duration(wait)}.",
                                  retry_after=wait)
    recent = [ts for ts in submissions if now - ts < DAY]
    if len(recent) >= cfg.max_attempts_per_day:
        wait = recent[0] + DAY - now
        return SubmitDecision(False, f"Не больше {cfg.max_attempts_per_day} попыток в сутки. "
                                     f"Следующая — через {human_duration(wait)}.", retry_after=wait)
    decision = SubmitDecision(True)
    if account_age_days is not None and account_age_days < cfg.min_account_age_days:
        decision.manual = True
        decision.flags.append(f"⚠️ Аккаунту {account_age_days:.1f} дн. (меньше "
                              f"{cfg.min_account_age_days:g}) — только ручная проверка")
    return decision


# ───────────────────── СОСТОЯНИЯ АНКЕТЫ ─────────────────────


class AlreadyDecided(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def next_status(current: str, action: str) -> str:
    """pending → accepted/rejected. Повторное решение — ошибка AlreadyDecided."""
    if action not in ("accept", "reject"):
        raise ValueError(action)
    if current != PENDING:
        raise AlreadyDecided(current)
    return ACCEPTED if action == "accept" else REJECTED
