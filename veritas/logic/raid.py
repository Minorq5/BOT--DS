"""Защита: детектор рейдов, скоринг подозрительности, анти-спам, anti-nuke. Без Discord.

Все классы принимают время явно (``now``), поэтому тестируются с поддельными часами.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import NukeConfig, RaidConfig, SpamConfig

# ───────────────────── ANTI-RAID ─────────────────────


class RaidDetector:
    """Скользящее окно входов: N входов за T секунд → всплеск."""

    def __init__(self, cfg: RaidConfig):
        self.cfg = cfg
        self.joins: deque[float] = deque()
        self.active = False
        self.started_at: float | None = None
        self.last_spike: float | None = None

    def _trim(self, now: float) -> None:
        while self.joins and now - self.joins[0] >= self.cfg.join_window_s:
            self.joins.popleft()

    def record_join(self, now: float) -> bool:
        """Учесть вход. True — если сейчас всплеск (окно заполнено до порога)."""
        self.joins.append(now)
        self._trim(now)
        spike = len(self.joins) >= self.cfg.join_threshold
        if spike:
            self.last_spike = now
        return spike

    def start(self, now: float) -> None:
        self.active = True
        self.started_at = now
        self.last_spike = self.last_spike or now

    def lift(self) -> None:
        self.active = False
        self.started_at = None
        self.joins.clear()

    def should_auto_lift(self, now: float) -> bool:
        if not self.active or self.cfg.auto_lift_minutes <= 0:
            return False
        last = self.last_spike or self.started_at or now
        return now - last >= self.cfg.auto_lift_minutes * 60


_LINKY = re.compile(r"(https?://|discord\.gg|discord(?:app)?\.com/invite|\.(?:com|ru|gg|io|xyz|net)\b)", re.I)


def name_skeleton(name: str) -> str:
    """Скелет ника для поиска шаблонов: без цифр, разделителей и диакритики."""
    s = unicodedata.normalize("NFKD", name or "").lower()
    return "".join(c for c in s if c.isalpha())


@dataclass
class Suspicion:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> None:
        self.score += points
        self.reasons.append(reason)


def suspicion(account_age_hours: float | None, default_avatar: bool, name: str,
              recent_names: Iterable[str], cfg: RaidConfig) -> Suspicion:
    s = Suspicion()
    new = account_age_hours is not None and account_age_hours < max(cfg.new_account_hours, 0)
    if new:
        s.add(2, f"новый аккаунт ({account_age_hours:.0f} ч)")
    if default_avatar and account_age_hours is not None and account_age_hours < 24 * 7:
        s.add(1, "аватар по умолчанию у свежего аккаунта")
    if _LINKY.search(name or ""):
        s.add(3, "ссылка в нике")
    skel = name_skeleton(name)
    if len(skel) >= 3:
        same = sum(1 for n in recent_names if name_skeleton(n) == skel)
        if same >= 2:
            s.add(2, f"шаблонный ник (ещё {same} похожих)")
    return s


# ───────────────────── ANTI-SPAM ─────────────────────

INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg)/[\w-]+", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.I)
EVERYONE_RE = re.compile(r"@(everyone|here)\b")


def link_hosts(content: str) -> list[str]:
    hosts = []
    for raw in URL_RE.findall(content or ""):
        url = raw if "://" in raw else "http://" + raw
        host = (urlsplit(url).hostname or "").lower()
        if host:
            hosts.append(host)
    return hosts


def host_allowed(host: str, whitelist: Iterable[str]) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in (w.lower() for w in whitelist))


def link_violation(content: str, whitelist: Iterable[str], *, allow_links: bool = False) -> str | None:
    if INVITE_RE.search(content or ""):
        return "приглашение на другой сервер"
    if allow_links:
        return None
    whitelist = list(whitelist)
    bad = [h for h in link_hosts(content) if not host_allowed(h, whitelist)]
    return f"ссылка ({bad[0]})" if bad else None


def _norm(content: str) -> str:
    return re.sub(r"\s+", " ", (content or "").strip().lower())


class SpamTracker:
    """Флуд, одинаковые сообщения в разных каналах, массовые упоминания."""

    def __init__(self, cfg: SpamConfig):
        self.cfg = cfg
        self.history: dict[int, deque[tuple[float, int, str]]] = defaultdict(deque)

    def check(self, user_id: int, now: float, channel_id: int, content: str, *, mentions: int = 0,
              text_available: bool = True) -> list[str]:
        cfg = self.cfg
        h = self.history[user_id]
        h.append((now, channel_id, _norm(content) if text_available else ""))
        keep = max(cfg.flood_window_s, cfg.duplicate_window_s)
        while h and now - h[0][0] > keep:
            h.popleft()
        reasons = []
        recent = [x for x in h if now - x[0] <= cfg.flood_window_s]
        if len(recent) > cfg.flood_messages:
            reasons.append(f"флуд ({len(recent)} сообщений за {cfg.flood_window_s} с)")
        if text_available:
            text = _norm(content)
            if len(text) >= 5:
                channels = {c for t, c, m in h if m == text and now - t <= cfg.duplicate_window_s}
                if len(channels) >= cfg.duplicate_channels:
                    reasons.append(f"одинаковое сообщение в {len(channels)} каналах")
            if EVERYONE_RE.search(content or ""):
                reasons.append("попытка упомянуть @everyone/@here")
        if mentions > cfg.max_mentions:
            reasons.append(f"массовые упоминания ({mentions})")
        return reasons

    def forget(self, user_id: int) -> None:
        self.history.pop(user_id, None)


class OffenseCounter:
    def __init__(self, window_s: float):
        self.window_s = window_s
        self.offenses: dict[int, deque[float]] = defaultdict(deque)

    def add(self, user_id: int, now: float) -> int:
        d = self.offenses[user_id]
        d.append(now)
        while d and now - d[0] > self.window_s:
            d.popleft()
        return len(d)


def ladder_action(offense_number: int, cfg: SpamConfig) -> tuple[str, int]:
    """Лестница: 1 — предупреждение, 2 — 10 минут, 3+ — 1 час."""
    steps = cfg.ladder_timeouts_s
    seconds = steps[min(offense_number, len(steps)) - 1]
    return ("warn", 0) if seconds <= 0 else ("timeout", seconds)


# ───────────────────── ANTI-NUKE ─────────────────────

DESTRUCTIVE_LABELS = {
    "channel_delete": "удаление канала",
    "role_delete": "удаление роли",
    "ban": "бан",
    "kick": "кик",
    "member_prune": "массовый кик (prune)",
    "webhook_create": "создание вебхука",
    "everyone_perms": "смена прав @everyone",
}


def classify_audit(action: str, *, target_is_everyone: bool = False, extra_is_everyone: bool = False,
                   permissions_changed: bool = False) -> str | None:
    """Свести запись журнала аудита к деструктивному действию или None."""
    if action in ("channel_delete", "role_delete", "ban", "kick", "member_prune", "webhook_create"):
        return action
    if action == "role_update" and target_is_everyone and permissions_changed:
        return "everyone_perms"
    if action in ("overwrite_create", "overwrite_update") and extra_is_everyone:
        return "everyone_perms"
    return None


class NukeDetector:
    """≥ threshold деструктивных действий одного пользователя за окно → срабатывание."""

    def __init__(self, cfg: NukeConfig):
        self.cfg = cfg
        self.events: dict[int, deque[tuple[float, str]]] = defaultdict(deque)

    def record(self, actor_id: int, kind: str, now: float) -> list[str] | None:
        """Учесть действие. При срабатывании вернуть список действий и сбросить счётчик."""
        d = self.events[actor_id]
        weight = self.cfg.threshold if kind == "member_prune" else 1
        for _ in range(weight):
            d.append((now, kind))
        while d and now - d[0][0] > self.cfg.window_s:
            d.popleft()
        if len(d) >= self.cfg.threshold:
            kinds = [k for _, k in d]
            d.clear()
            return kinds
        return None
