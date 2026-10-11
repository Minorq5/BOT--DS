"""Порядок ролей: чистый расчёт новых позиций."""

from __future__ import annotations

from collections.abc import Sequence


def plan_role_positions(order: Sequence[str], current: dict[str, int],
                        bot_top: int) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """Выстроить известные роли в порядке ``order`` (сверху вниз).

    Двигаем только роли ниже роли бота и только между позициями, которые они уже
    занимают, — посторонние роли остаются на месте.
    Возвращает (ключ → новая позиция для изменившихся, пары (выше, ниже), которые
    остались в неверном порядке и требуют ручного перетаскивания).
    """
    present = [k for k in order if k in current]
    movable = [k for k in present if current[k] < bot_top]
    slots = sorted((current[k] for k in movable), reverse=True)
    final = dict(current)
    for key, slot in zip(movable, slots, strict=True):
        final[key] = slot
    changes = {k: final[k] for k in movable if final[k] != current[k]}
    problems = [(a, b) for a, b in zip(present, present[1:], strict=False) if final[a] <= final[b]]
    return changes, problems
