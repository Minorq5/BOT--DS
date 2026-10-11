"""Счётчик активности: только число сообщений в день, без содержимого."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


class ActivityCounter:
    def __init__(self):
        self.pending: Counter[tuple[int, str]] = Counter()

    def add(self, user_id: int, ts: float) -> None:
        self.pending[(user_id, day_key(ts))] += 1

    def drain(self) -> list[tuple[int, str, int]]:
        items = [(uid, day, n) for (uid, day), n in self.pending.items()]
        self.pending.clear()
        return items
