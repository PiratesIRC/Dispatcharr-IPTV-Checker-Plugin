"""Integration coverage for channel-group filtering in load_groups_action.

Drives the real action with the ORM seams monkeypatched (same pattern as
test_restore_and_black.py), so the wiring is verified end-to-end and not just
the pure matcher.

Two halves:

  * The MODE tests exercise the current settings, `channel_groups` plus
    `channel_groups_mode`.
  * The MIGRATION tests drive the action with only the OLD settings present,
    `group_names` and `group_names_exclude`, which is exactly what an upgraded
    install has in its database. Dispatcharr never prunes a stored setting when
    its field is removed, so those values persist and must keep producing the
    same scope. Their assertions on group_ids are unchanged from when those
    settings were the live ones, which is what makes them a regression test
    rather than a restatement.
"""


def _wire(plugin, monkeypatch, groups):
    """Monkeypatch the ORM seams; return a dict that captures the group_ids
    that load_groups_action ultimately asks _get_all_channels for."""
    captured = {}
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: groups)

    def fake_get_all_channels(logger, group_ids=None):
        captured["group_ids"] = group_ids
        return []

    monkeypatch.setattr(plugin, "_get_all_channels", fake_get_all_channels)
    monkeypatch.setattr(plugin, "_get_channel_streams_bulk", lambda *a, **k: {})
    return captured


_GROUPS = [
    {"id": 1, "name": "US-Sports"},
    {"id": 2, "name": "US-PPV-1"},
    {"id": 3, "name": "US-PPV-2"},
    {"id": 4, "name": "Movies"},
]


# ---- current settings: one list plus a mode ------------------------------

def test_include_mode_keeps_only_listed(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "US-*", "channel_groups_mode": "include"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 2, 3}


def test_exclude_mode_drops_listed(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "US-PPV-*", "channel_groups_mode": "exclude"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 4}


def test_empty_list_loads_every_group_in_both_modes(plugin, monkeypatch, quiet_logger):
    for mode in ("include", "exclude"):
        captured = _wire(plugin, monkeypatch, _GROUPS)
        res = plugin.load_groups_action(
            {"channel_groups": "", "channel_groups_mode": mode}, quiet_logger)
        assert res["status"] == "ok", mode
        assert captured["group_ids"] == {1, 2, 3, 4}, mode


def test_exclude_everything_errors(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "*", "channel_groups_mode": "exclude"}, quiet_logger)
    assert res["status"] == "error"
    assert "nothing to check" in res["message"].lower()
    assert "group_ids" not in captured  # never reached the channel fetch


def test_include_matching_nothing_errors(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "Nope-*", "channel_groups_mode": "include"}, quiet_logger)
    assert res["status"] == "error"
    assert "group_ids" not in captured


def test_unknown_mode_behaves_as_include(plugin, monkeypatch, quiet_logger):
    """A stored mode this build does not understand must keep a list of WANTED
    groups meaning wanted, not invert it into a list of skipped ones."""
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "US-*", "channel_groups_mode": "nonsense"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 2, 3}


# ---- migration: only the OLD settings present in the database ------------

def test_legacy_include_and_exclude_together(plugin, monkeypatch, quiet_logger):
    """The one case a single list plus a mode cannot express. Both stay applied,
    so an upgrade cannot widen the scope of a destructive action."""
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1}  # US-PPV-1/2 excluded, US-Sports kept


def test_legacy_exclude_from_all(plugin, monkeypatch, quiet_logger):
    """The live configuration on the operator's box: blank include, exclude the
    PPV groups. Must keep meaning every group except those."""
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "", "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 4}


def test_legacy_everything_excluded_errors(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": "US-*"}, quiet_logger)
    assert res["status"] == "error"
    assert "nothing to check" in res["message"].lower()
    assert "group_ids" not in captured


def test_legacy_blank_exclude_is_noop(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": ""}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 2, 3}


def test_new_setting_overrides_stale_legacy_values(plugin, monkeypatch, quiet_logger):
    """Once anything is entered in the new box, the stale values still sitting
    in the database are ignored entirely."""
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "Movies", "channel_groups_mode": "include",
         "group_names": "US-*", "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {4}


def test_wildcard_escape_hatch_beats_a_stale_legacy_exclude(plugin, monkeypatch, quiet_logger):
    """A legacy value survives in the database forever. An explicit `*` in
    include mode is how an operator asks for every group anyway."""
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"channel_groups": "*", "channel_groups_mode": "include",
         "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 2, 3, 4}
