import discord

from veritas import config
from veritas.config import Settings, load_dotenv, override_dataclass


def test_sixteen_channels_with_unique_keys():
    chans = config.all_channels()
    assert len(chans) == 16
    keys = [c.key for _, c in chans]
    assert len(set(keys)) == 16
    names = [c.name for _, c in chans]
    assert len(set(names)) == 16


def test_permission_names_are_valid_discord_flags():
    valid = discord.Permissions.VALID_FLAGS
    for perms in config.ROLE_PERMS.values():
        assert set(perms) <= set(valid)
    assert set(config.SAFE_EVERYONE) <= set(valid)
    assert set(config.BOT_OVERWRITE) <= set(valid)
    for _, c in config.all_channels():
        for kw in c.roles.values():
            assert set(kw) <= set(valid)


def test_admin_only_for_founder():
    for key, perms in config.ROLE_PERMS.items():
        for p in config.FOUNDER_ONLY_PERMS:
            if perms.get(p):
                assert key == config.FOUNDER
    holders = [k for k, p in config.ROLE_PERMS.items() if p.get("manage_channels")]
    assert holders == [config.HEAD]


def test_all_roles_have_clearance_or_are_marks():
    for key in config.ROLE_ORDER:
        assert key in config.ROLE_CLEARANCE or key == config.LIAR


def test_settings_from_env_defaults_and_parsing():
    s = Settings.from_env({
        "DISCORD_TOKEN": "secret-token", "GUILD_ID": "123", "ALLOWED_SITE_DOMAINS": "A.example.com, b.org",
        "VERIFY_MODE": "AUTO", "RESTORE_ON_REJOIN": "true",
    })
    assert s.guild_id == 123
    assert s.allowed_site_domains == ("a.example.com", "b.org")
    assert s.verify_mode == "auto"
    assert s.restore_on_rejoin is True
    assert s.api_host == "127.0.0.1"
    assert "secret-token" not in repr(s)
    assert Settings.from_env({"VERIFY_MODE": "weird"}).verify_mode == "manual"


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nVX_A=1\nVX_B='quoted value'\nVX_C=x # inline\nVX_D=\n", encoding="utf-8")
    monkeypatch.setenv("VX_A", "keep")
    monkeypatch.delenv("VX_B", raising=False)
    monkeypatch.delenv("VX_C", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["VX_A"] == "keep"
    assert os.environ["VX_B"] == "quoted value"
    assert os.environ["VX_C"] == "x"
    assert os.environ["VX_D"] == ""
    for k in ("VX_B", "VX_C", "VX_D"):
        monkeypatch.delenv(k, raising=False)


def test_override_dataclass_casts_types():
    cfg = override_dataclass(config.RAID, {"join_threshold": "5", "disable_invites": "false",
                                           "lockdown_action": "kick", "unknown": "1"})
    assert cfg.join_threshold == 5
    assert cfg.disable_invites is False
    assert cfg.lockdown_action == "kick"
