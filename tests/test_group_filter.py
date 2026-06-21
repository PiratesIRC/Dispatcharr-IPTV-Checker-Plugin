"""Integration coverage for include/exclude group filtering in load_groups_action.

Drives the real action with the ORM seams monkeypatched (same pattern as
test_restore_and_black.py), so the exclude wiring — exclude-wins, the
all-excluded error, and the single post-exclusion target_group_ids compute — is
verified end-to-end, not just the pure matcher.
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


def test_load_groups_exclude_wins(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1}  # US-PPV-1/2 excluded, US-Sports kept


def test_load_groups_exclude_from_all(plugin, monkeypatch, quiet_logger):
    # Blank include = all groups; exclude removes the PPV ones.
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "", "group_names_exclude": "US-PPV-*"}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 4}


def test_load_groups_all_excluded_errors(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": "US-*"}, quiet_logger)
    assert res["status"] == "error"
    assert "excluded" in res["message"].lower()
    assert "group_ids" not in captured  # never reached the channel fetch


def test_load_groups_blank_exclude_is_noop(plugin, monkeypatch, quiet_logger):
    captured = _wire(plugin, monkeypatch, _GROUPS)
    res = plugin.load_groups_action(
        {"group_names": "US-*", "group_names_exclude": ""}, quiet_logger)
    assert res["status"] == "ok"
    assert captured["group_ids"] == {1, 2, 3}
