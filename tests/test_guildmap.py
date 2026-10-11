from veritas import config
from veritas.guildmap import resolve_role_ids


def test_resolve_by_id_and_name():
    roles = [(999, "👁 Основатель")] + [(rid, key) for key, rid in config.ROLE_IDS.items()] + [
        (555, "📖 Последователь")]
    ids, missing = resolve_role_ids(roles)
    assert missing == []
    assert ids[config.FOUNDER] == 999
    assert ids[config.FOLL] == 555
    assert ids[config.HEAD] == config.ROLE_IDS[config.HEAD]


def test_missing_ids_reported_and_follower_optional():
    ids, missing = resolve_role_ids([(1, "random")])
    assert set(missing) == set(config.ROLE_IDS)
    assert config.FOLL not in ids


def test_cached_follower_id_preferred():
    roles = [(10, "последователи-старые"), (11, "📖 Последователь")]
    ids, _ = resolve_role_ids(roles, cached={config.FOLL: 11})
    assert ids[config.FOLL] == 11
    ids, _ = resolve_role_ids(roles, cached={config.FOLL: 12})
    assert ids[config.FOLL] == 10


def test_name_search_is_case_insensitive():
    ids, _ = resolve_role_ids([(5, "ОСНОВАТЕЛЬ")], role_ids={})
    assert ids[config.FOUNDER] == 5
