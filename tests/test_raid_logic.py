import pytest

from veritas.config import NukeConfig, RaidConfig, SpamConfig
from veritas.logic.raid import (
    NukeDetector,
    OffenseCounter,
    RaidDetector,
    SpamTracker,
    classify_audit,
    host_allowed,
    ladder_action,
    link_violation,
    name_skeleton,
    suspicion,
)

RAID = RaidConfig()  # 8 входов за 20 с


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ───── рейд-детектор на поддельных часах

def test_raid_triggers_inside_window():
    d = RaidDetector(RAID)
    results = [d.record_join(1000 + i * 2.7) for i in range(8)]  # 8 входов за 18.9 с
    assert results[-1] is True and not any(results[:-1])


def test_raid_not_triggered_on_boundary():
    d = RaidDetector(RAID)
    times = [1000 + i * (20 / 7) for i in range(8)]  # первый и восьмой ровно через 20 с
    assert not any(d.record_join(t) for t in times)


def test_seven_joins_never_trigger():
    d = RaidDetector(RAID)
    assert not any(d.record_join(1000 + i * 0.1) for i in range(7))


def test_auto_lift_after_quiet_period():
    d = RaidDetector(RAID)
    for i in range(8):
        d.record_join(1000 + i)
    d.start(1007)
    assert not d.should_auto_lift(1007 + RAID.auto_lift_minutes * 60 - 1)
    # новый всплеск продлевает локдаун
    for i in range(8):
        d.record_join(1500 + i)
    assert not d.should_auto_lift(1007 + RAID.auto_lift_minutes * 60 + 1)
    assert d.should_auto_lift(1507 + RAID.auto_lift_minutes * 60)
    d.lift()
    assert not d.active and not d.should_auto_lift(99999)


def test_auto_lift_disabled_when_zero():
    d = RaidDetector(RaidConfig(auto_lift_minutes=0))
    d.start(0)
    assert not d.should_auto_lift(10**9)


def test_suspicion_scoring():
    clean = suspicion(24 * 365, False, "Вейл", [], RAID)
    assert clean.score == 0
    young = suspicion(2, True, "Вейл", [], RAID)
    assert young.score == 3 and len(young.reasons) == 2
    link = suspicion(24 * 365, False, "join discord.gg/abc", [], RAID)
    assert link.score == 3
    pattern = suspicion(24 * 365, False, "raider777", ["raider1", "raider22"], RAID)
    assert pattern.score == 2
    assert name_skeleton("Raider_123") == "raider"


# ───── анти-спам

SPAM = SpamConfig()


def test_flood_over_threshold():
    t = SpamTracker(SPAM)
    res = [t.check(1, 100 + i * 0.5, 10, f"msg {i}") for i in range(SPAM.flood_messages + 1)]
    assert not any(res[:-1]) and "флуд" in res[-1][0]


def test_duplicates_across_channels():
    t = SpamTracker(SPAM)
    assert t.check(1, 100, 10, "купи золото дёшево") == []
    assert t.check(1, 110, 11, "Купи   золото дёшево") == []
    assert "одинаковое" in t.check(1, 120, 12, "купи золото дёшево")[0]


def test_mass_mentions_and_everyone():
    t = SpamTracker(SPAM)
    assert "массовые" in t.check(1, 0, 1, "hi", mentions=6)[0]
    assert "everyone" in t.check(2, 0, 1, "внимание @everyone")[0]
    assert t.check(3, 0, 1, "hi", mentions=5) == []


def test_text_checks_skipped_without_intent():
    t = SpamTracker(SPAM)
    assert t.check(1, 0, 1, "@everyone", text_available=False) == []


@pytest.mark.parametrize("content,whitelist,expected", [
    ("заходи discord.gg/abc", [], "приглашение"),
    ("https://discord.com/invite/xyz", ["discord.com"], "приглашение"),
    ("смотри https://veritas.example.com/dossier/1", ["veritas.example.com"], None),
    ("смотри https://sub.veritas.example.com/x", ["veritas.example.com"], None),
    ("смотри https://evil.com/x", ["veritas.example.com"], "ссылка"),
    ("www.evil.com", [], "ссылка"),
    ("без ссылок", [], None),
])
def test_link_violation(content, whitelist, expected):
    res = link_violation(content, whitelist)
    assert (res is None) if expected is None else (expected in res)


def test_link_exempt_channel_still_blocks_invites():
    assert link_violation("https://evil.com", [], allow_links=True) is None
    assert link_violation("discord.gg/x", [], allow_links=True) is not None


def test_host_allowed_not_suffix_trick():
    assert not host_allowed("evilveritas.example.com", ["veritas.example.com"])


def test_ladder():
    assert ladder_action(1, SPAM) == ("warn", 0)
    assert ladder_action(2, SPAM) == ("timeout", 600)
    assert ladder_action(3, SPAM) == ("timeout", 3600)
    assert ladder_action(9, SPAM) == ("timeout", 3600)


def test_offense_window():
    c = OffenseCounter(100)
    assert c.add(1, 0) == 1
    assert c.add(1, 50) == 2
    assert c.add(1, 101) == 2  # первое выпало, второе осталось


# ───── anti-nuke на потоке событий

NUKE = NukeConfig()  # ≥3 за 60 с


def test_nuke_triggers_on_third_action_in_window():
    d = NukeDetector(NUKE)
    assert d.record(1, "channel_delete", 0) is None
    assert d.record(1, "role_delete", 10) is None
    assert d.record(1, "ban", 59) == ["channel_delete", "role_delete", "ban"]
    assert d.record(1, "ban", 60) is None  # счётчик сброшен после срабатывания


def test_nuke_spread_out_does_not_trigger():
    d = NukeDetector(NUKE)
    assert d.record(1, "channel_delete", 0) is None
    assert d.record(1, "channel_delete", 40) is None
    assert d.record(1, "channel_delete", 101) is None


def test_nuke_counts_per_user():
    d = NukeDetector(NUKE)
    for uid in (1, 2, 3):
        assert d.record(uid, "kick", 0) is None
        assert d.record(uid, "kick", 1) is None


def test_prune_triggers_immediately():
    assert NukeDetector(NUKE).record(1, "member_prune", 0) is not None


def test_classify_audit():
    assert classify_audit("channel_delete") == "channel_delete"
    assert classify_audit("webhook_create") == "webhook_create"
    assert classify_audit("role_update", target_is_everyone=True, permissions_changed=True) == "everyone_perms"
    assert classify_audit("role_update", target_is_everyone=False, permissions_changed=True) is None
    assert classify_audit("overwrite_update", extra_is_everyone=True) == "everyone_perms"
    assert classify_audit("overwrite_update", extra_is_everyone=False) is None
    assert classify_audit("message_delete") is None
