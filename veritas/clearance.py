"""Допуски и правила «кто кого может». Чистая логика без Discord.

Роли везде передаются ключами из ``config`` (``FOUNDER``, ``HEAD``…).
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import (
    ARCH,
    COUNCIL,
    EXILE,
    FOLL,
    FOUNDER,
    HEAD,
    INIT,
    LIAR,
    LIAR_CEILING,
    ROLE_CLEARANCE,
    STRANGER,
    TRUST,
)

# ───────────────────── ДОПУСК ─────────────────────


def clearance_level(role_keys: Iterable[str]) -> int:
    """Допуск человека = максимум по ролям; Отлучённый → 0; Лжец → min(уровень, 2).

    Человек без известных ролей получает 0 (он ещё не прошёл даже вход).
    """
    keys = set(role_keys)
    if EXILE in keys:
        return 0
    level = max((ROLE_CLEARANCE[k] for k in keys if k in ROLE_CLEARANCE), default=0)
    if LIAR in keys:
        level = min(level, LIAR_CEILING)
    return level


# ───────────────────── РАНГИ ─────────────────────

# Сверху вниз. Метки (Лжец, Отлучённый) рангами не являются.
RANKS: tuple[str, ...] = (FOUNDER, HEAD, COUNCIL, ARCH, TRUST, INIT, FOLL, STRANGER)
RANK_ROLES = frozenset(RANKS)
_RANK_INDEX = {k: len(RANKS) - 1 - i for i, k in enumerate(RANKS)}  # STRANGER=0 … FOUNDER=7

# Ранги, на которые нельзя ставить метки.
PROTECTED = frozenset({FOUNDER, HEAD, COUNCIL})

NEXT_RANK = {None: FOLL, STRANGER: FOLL, FOLL: INIT, INIT: TRUST, TRUST: COUNCIL,
             ARCH: COUNCIL, COUNCIL: HEAD, HEAD: None, FOUNDER: None}
PREV_RANK = {HEAD: COUNCIL, COUNCIL: TRUST, ARCH: INIT, TRUST: INIT, INIT: FOLL,
             FOLL: STRANGER, STRANGER: None, None: None, FOUNDER: None}


def rank_index(key: str | None) -> int:
    return -1 if key is None else _RANK_INDEX[key]


def member_rank(role_keys: Iterable[str]) -> str | None:
    """Старший ранг человека (или None)."""
    best = None
    for k in role_keys:
        if k in _RANK_INDEX and rank_index(k) > rank_index(best):
            best = k
    return best


def required_rank(role: str, head_can_appoint_council: bool = False) -> str | None:
    """Минимальный ранг, который может выдать или снять эту роль. None — никто."""
    table = {
        STRANGER: TRUST,
        FOLL: TRUST,
        INIT: ARCH,
        TRUST: COUNCIL,
        ARCH: COUNCIL,
        COUNCIL: HEAD if head_can_appoint_council else FOUNDER,
        HEAD: FOUNDER,
        FOUNDER: None,
    }
    return table.get(role)


def _meets(actor_rank: str | None, need: str | None) -> bool:
    return need is not None and rank_index(actor_rank) >= rank_index(need)


def can_set_rank(actor_keys: Iterable[str], target_keys: Iterable[str], new_rank: str,
                 head_can_appoint_council: bool = False) -> tuple[bool, str]:
    """Может ли actor поставить target ранг new_rank. Возвращает (можно, причина отказа)."""
    actor_keys, target_keys = set(actor_keys), set(target_keys)
    if new_rank not in RANK_ROLES:
        return False, "Это не ранг."
    if new_rank == FOUNDER:
        return False, "Роль Основателя бот не выдаёт."
    actor = member_rank(actor_keys)
    target = member_rank(target_keys)
    if actor_keys & {EXILE, LIAR}:
        return False, "С меткой нельзя распоряжаться чужими допусками."
    if rank_index(actor) <= rank_index(target):
        return False, "Нельзя трогать того, кто равен тебе или выше."
    if rank_index(actor) <= rank_index(new_rank):
        return False, "Нельзя выдать роль, равную своей или выше."
    if EXILE in target_keys:
        return False, "Человек отлучён. Сначала /restore."
    if LIAR in target_keys and rank_index(new_rank) > rank_index(FOLL):
        return False, "На человеке метка Лжеца: выше Последователя нельзя. Сначала /restore."
    if not _meets(actor, required_rank(new_rank, head_can_appoint_council)):
        return False, "Твоего ранга недостаточно для этой роли."
    if target is not None and target != new_rank and not _meets(
            actor, required_rank(target, head_can_appoint_council)):
        return False, "Твоего ранга недостаточно, чтобы снять текущую роль."
    if target == new_rank:
        return False, "У человека уже этот ранг."
    return True, ""


def plan_set_rank(target_keys: Iterable[str], new_rank: str) -> tuple[set[str], set[str]]:
    """(снять, выдать): ранги взаимоисключающие, Основатель не трогается."""
    keys = set(target_keys)
    remove = {k for k in keys if k in RANK_ROLES and k not in (FOUNDER, new_rank)}
    add = set() if new_rank in keys else {new_rank}
    return remove, add


# ───────────────────── МЕТКИ ─────────────────────


def can_mark(actor_keys: Iterable[str], target_keys: Iterable[str]) -> tuple[bool, str]:
    actor_keys, target_keys = set(actor_keys), set(target_keys)
    if target_keys & PROTECTED:
        return False, "Метки не накладываются на Основателя и Совет."
    if rank_index(member_rank(actor_keys)) <= rank_index(member_rank(target_keys)):
        return False, "Нельзя трогать того, кто равен тебе или выше."
    return True, ""


def plan_liar(target_keys: Iterable[str]) -> tuple[set[str], set[str]]:
    """Лжец: снять всё выше Последователя, выдать метку."""
    keys = set(target_keys)
    remove = {k for k in keys if k in RANK_ROLES and rank_index(k) > rank_index(FOLL)}
    add = set() if LIAR in keys else {LIAR}
    return remove, add


def plan_exile(target_keys: Iterable[str], managed: Iterable[str]) -> tuple[set[str], set[str]]:
    """Отлучение: снять все управляемые роли, оставить только Отлучённого."""
    keys = set(target_keys)
    managed = set(managed)
    remove = {k for k in keys if k in managed and k not in (EXILE, FOUNDER)}
    add = set() if EXILE in keys else {EXILE}
    return remove, add


def plan_restore(target_keys: Iterable[str], snapshot_keys: Iterable[str]) -> tuple[set[str], set[str]]:
    """Снять метки и вернуть сохранённые роли. Без ранга человек становится Незнакомцем."""
    keys = set(target_keys)
    remove = keys & {LIAR, EXILE}
    back = {k for k in snapshot_keys if k not in (LIAR, EXILE, FOUNDER)}
    final = (keys - remove) | back
    if member_rank(final) is None:
        back.add(STRANGER)
    add = back - keys
    return remove, add


# ───────────────────── ТАЙМ-АУТЫ ─────────────────────


def can_timeout(actor_keys: Iterable[str], target_keys: Iterable[str], seconds: int,
                trusted_max_s: int, discord_max_s: int) -> tuple[bool, str]:
    actor_keys, target_keys = set(actor_keys), set(target_keys)
    actor_level = clearance_level(actor_keys)
    if actor_level < 4:
        return False, "Тайм-ауты выдают Доверенные и выше."
    if rank_index(member_rank(actor_keys)) <= rank_index(member_rank(target_keys)):
        return False, "Нельзя трогать того, кто равен тебе или выше."
    if seconds <= 0:
        return False, "Длительность должна быть больше нуля."
    if seconds > discord_max_s:
        return False, "Discord не позволяет тайм-аут дольше 28 дней."
    if actor_level < 5 and seconds > trusted_max_s:
        hours = trusted_max_s // 3600
        return False, f"Доверенные и Архивариусы могут дать тайм-аут не больше {hours} ч."
    return True, ""
