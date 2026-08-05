"""Black/blank flag + channel-restore: pure helpers and action behavior."""

import json
import logging


# ---- Task 1: predicates + tag derivation --------------------------------

def test_is_dead_nonblack(pmod):
    P = pmod.Plugin
    assert P._is_dead_nonblack({"status": "Dead", "error_type": "Timeout"}) is True
    assert P._is_dead_nonblack({"status": "Dead", "error_type": "Black Screen"}) is False
    assert P._is_dead_nonblack({"status": "Dead"}) is True  # no error_type -> not black
    assert P._is_dead_nonblack({"status": "Alive"}) is False


def test_is_black_screen(pmod):
    P = pmod.Plugin
    assert P._is_black_screen({"status": "Dead", "error_type": "Black Screen"}) is True
    assert P._is_black_screen({"status": "Dead", "error_type": "Timeout"}) is False
    assert P._is_black_screen({"status": "Alive", "error_type": "Black Screen"}) is False


def test_extract_format_tags(pmod):
    P = pmod.Plugin
    assert P._extract_format_tags("{name} [DEAD]") == ["DEAD"]
    assert P._extract_format_tags("[X] {name} [DEAD]") == ["X", "DEAD"]
    assert P._extract_format_tags("") == []
    assert P._extract_format_tags(None) == []


def test_derive_status_tags_strips_only_status(pmod):
    P = pmod.Plugin
    settings = {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
    }
    rx = P._derive_status_tags(settings)
    assert rx.search("ESPN [DEAD]")
    assert rx.search("ESPN [Slow]")
    assert rx.search("ESPN [Blank]")
    assert not rx.search("ESPN [HD]")  # quality tag is NOT a status tag


def test_derive_strippable_tags_strips_status_and_quality(pmod):
    P = pmod.Plugin
    settings = {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
        "video_format_suffixes": "UHD, FHD, HD, SD, Unknown",
    }
    rx = P._derive_strippable_tags(settings)
    for name in ("ESPN [DEAD]", "ESPN [Slow]", "ESPN [Blank]", "ESPN [HD]", "ESPN [UHD]"):
        assert rx.sub("", name).rstrip() == "ESPN", name
    # Stacked trailing tags collapse to the clean base.
    assert rx.sub("", "ESPN [HD] [Blank]").rstrip() == "ESPN"
    # Custom label from a user-edited format is honored.
    rx2 = P._derive_strippable_tags({"dead_rename_format": "{name} [GONE]"})
    assert rx2.sub("", "ESPN [GONE]").rstrip() == "ESPN"


def test_derive_tags_case_insensitive_and_trailing_only(pmod):
    P = pmod.Plugin
    rx = P._derive_strippable_tags({"dead_rename_format": "{name} [DEAD]"})
    assert rx.sub("", "ESPN [dead]").rstrip() == "ESPN"            # case-insensitive
    assert rx.sub("", "[DEAD] ESPN Sports") == "[DEAD] ESPN Sports"  # not trailing -> untouched


# ---- Task 2: pure planners ----------------------------------------------

def _settings():
    return {
        "dead_rename_format": "{name} [DEAD]",
        "low_framerate_rename_format": "{name} [Slow]",
        "black_screen_rename_format": "{name} [Blank]",
        "video_format_suffixes": "UHD, FHD, HD, SD, Unknown",
    }


def test_restore_plan_strips_tag_and_restores_group(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    alive = {10: "ESPN [DEAD]", 11: "TNT [Blank]"}
    state = {"10": {"original_group_id": 5, "original_group_name": "USA"},
             "11": {"original_group_id": 7, "original_group_name": "Movies"}}
    plan = P._compute_restore_plan(alive, state, strip_re, status_re, existing_group_ids={5, 7})
    assert {"id": 10, "name": "ESPN"} in plan["name_updates"]
    assert {"id": 11, "name": "TNT"} in plan["name_updates"]
    assert {"id": 10, "channel_group_id": 5} in plan["group_updates"]
    assert plan["entries_to_clear"] == {"10", "11"}
    assert plan["missing_group_ids"] == {}


def test_restore_plan_ignores_unmarked_alive(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Healthy channel with only a quality tag, never marked, no state -> untouched.
    plan = P._compute_restore_plan({20: "CNN [HD]"}, {}, strip_re, status_re, existing_group_ids=set())
    assert plan["name_updates"] == []
    assert plan["group_updates"] == []
    assert plan["entries_to_clear"] == set()


def test_restore_plan_eligible_by_state_even_without_tag(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Name already clean but state exists (was moved, manually renamed) -> move back.
    plan = P._compute_restore_plan({30: "Fox"}, {"30": {"original_group_id": 9}},
                                   strip_re, status_re, existing_group_ids={9})
    assert plan["name_updates"] == []  # nothing to strip
    assert {"id": 30, "channel_group_id": 9} in plan["group_updates"]
    assert plan["entries_to_clear"] == {"30"}


def test_restore_plan_missing_group_keeps_name_drops_entry(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    plan = P._compute_restore_plan({40: "ABC [DEAD]"}, {"40": {"original_group_id": 99}},
                                   strip_re, status_re, existing_group_ids={1, 2})
    assert {"id": 40, "name": "ABC"} in plan["name_updates"]
    assert plan["group_updates"] == []
    assert plan["missing_group_ids"] == {"40": 99}
    assert plan["entries_to_clear"] == {"40"}


def test_restore_plan_name_only_tag_not_emptied(pmod):
    P = pmod.Plugin
    strip_re = P._derive_strippable_tags(_settings())
    status_re = P._derive_status_tags(_settings())
    # Name that is ONLY a tag would strip to empty -> skip the rename, keep original.
    plan = P._compute_restore_plan({50: "[DEAD]"}, {}, strip_re, status_re, existing_group_ids=set())
    assert plan["name_updates"] == []


def test_capture_state_skips_existing_and_managed(pmod):
    P = pmod.Plugin
    current_group = {1: 100, 2: 200, 3: 300}
    group_names = {100: "USA Sports", 200: "Graveyard", 300: "Movies"}
    managed = ["Graveyard", "Slow", "Black Screens"]
    existing = {"3": {"original_group_id": 300}}  # already tracked
    new = P._compute_capture_state([1, 2, 3], current_group, group_names, managed, existing, "T0")
    assert "1" in new and new["1"]["original_group_id"] == 100
    assert "2" not in new   # currently in a managed group -> not recorded
    assert "3" not in new   # already tracked -> not overwritten
    assert new["1"]["original_group_name"] == "USA Sports"
    assert new["1"]["moved_at"] == "T0"


def test_capture_state_skips_channel_with_no_group(pmod):
    P = pmod.Plugin
    # gid None -> nothing to restore to; do not record (name-strip still covers it on restore).
    new = P._compute_capture_state([1], {1: None}, {}, ["Graveyard"], {}, "T0")
    assert new == {}


# ---- Task 3: capture wrapper + black actions ----------------------------

def _logger():
    lg = logging.getLogger("iptv_checker.tests.t3")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


def _write(plugin, results):
    with open(plugin.results_file, "w") as f:
        json.dump(results, f)


def test_capture_original_state_writes_file(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(plugin, "_get_all_channels",
                        lambda logger: [{"id": 1, "name": "ESPN", "channel_group_id": 100}])
    monkeypatch.setattr(plugin, "_get_all_groups",
                        lambda logger: [{"id": 100, "name": "USA Sports"}, {"id": 9, "name": "Graveyard"}])
    settings = {"move_to_group_name": "Graveyard", "move_low_framerate_group": "Slow",
                "move_black_screen_group": "Black Screens"}
    plugin._capture_original_state([1], settings, _logger())
    state = json.load(open(plugin.channel_state_file))
    assert state["1"]["original_group_id"] == 100
    assert state["1"]["original_group_name"] == "USA Sports"


def test_rename_dead_excludes_black(plugin, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: captured.setdefault("p", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "A", "status": "Dead", "error_type": "Timeout"},
        {"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"},
    ])
    res = plugin.rename_channels_action({"dead_rename_format": "{name} [DEAD]"}, _logger())
    assert res["status"] == "ok"
    ids = {p["id"] for p in captured["p"]}
    assert ids == {1}  # black channel 2 excluded


def test_rename_black_targets_only_black(plugin, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: captured.setdefault("p", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "A", "status": "Dead", "error_type": "Timeout"},
        {"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"},
    ])
    res = plugin.rename_black_screen_channels_action({"black_screen_rename_format": "{name} [Blank]"}, _logger())
    assert res["status"] == "ok"
    assert captured["p"] == [{"id": 2, "name": "B [Blank]"}]


def test_move_black_captures_then_moves(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    calls = {}
    monkeypatch.setattr(plugin, "_capture_original_state",
                        lambda ids, s, lg: calls.setdefault("captured", set(ids)))
    monkeypatch.setattr(plugin, "_get_or_create_group",
                        lambda name, logger: type("G", (), {"id": 77})())
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: calls.setdefault("moved", payload) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    _write(plugin, [{"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Black Screen"}])
    res = plugin.move_black_screen_channels_action({"move_black_screen_group": "Black Screens"}, _logger())
    assert res["status"] == "ok"
    assert calls["captured"] == {2}
    assert calls["moved"] == [{"id": 2, "channel_group_id": 77}]


# ---- Task 4: restore action ---------------------------------------------

def test_restore_action_strips_and_moves_back(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    with open(plugin.channel_state_file, "w") as f:
        json.dump({"1": {"original_group_id": 100, "original_group_name": "USA"}}, f)
    _write(plugin, [
        {"channel_id": 1, "channel_name": "ESPN", "status": "Alive", "error_type": None},
        {"channel_id": 2, "channel_name": "Dead1", "status": "Dead", "error_type": "Timeout"},
    ])
    monkeypatch.setattr(plugin, "_get_all_channels",
                        lambda logger: [{"id": 1, "name": "ESPN [DEAD]"}, {"id": 2, "name": "Dead1 [DEAD]"}])
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: [{"id": 100, "name": "USA"}])
    payloads = []
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: payloads.append((fields[0], payload)) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)

    settings = {"dead_rename_format": "{name} [DEAD]", "video_format_suffixes": "UHD, FHD, HD, SD, Unknown"}
    res = plugin.restore_channels_action(settings, _logger())
    assert res["status"] == "ok"
    assert res["restored"] == 1
    by_field = dict(payloads)
    assert by_field["name"] == [{"id": 1, "name": "ESPN"}]
    assert by_field["channel_group_id"] == [{"id": 1, "channel_group_id": 100}]
    # State entry cleared after restore.
    assert json.load(open(plugin.channel_state_file)) == {}


def test_restore_action_status_tag_without_state(plugin, tmp_path, monkeypatch):
    # Channel was renamed [DEAD] but never moved (no state entry): strip the name,
    # do NOT move, nothing to clear. Common "renamed but never moved" path.
    plugin.channel_state_file = str(tmp_path / "state.json")
    with open(plugin.channel_state_file, "w") as f:
        json.dump({}, f)
    _write(plugin, [{"channel_id": 7, "channel_name": "Sky", "status": "Alive"}])
    monkeypatch.setattr(plugin, "_get_all_channels", lambda logger: [{"id": 7, "name": "Sky [DEAD]"}])
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: [{"id": 1, "name": "Sports"}])
    payloads = []
    monkeypatch.setattr(plugin, "_bulk_update_channels",
                        lambda payload, fields, logger: payloads.append((fields[0], payload)) or len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    res = plugin.restore_channels_action({"dead_rename_format": "{name} [DEAD]"}, _logger())
    assert res["status"] == "ok"
    assert res["restored"] == 1
    by_field = dict(payloads)
    assert by_field["name"] == [{"id": 7, "name": "Sky"}]
    assert by_field["channel_group_id"] == []  # no state -> no move
    assert json.load(open(plugin.channel_state_file)) == {}


def test_restore_action_no_recovered(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    _write(plugin, [{"channel_id": 9, "channel_name": "CNN", "status": "Alive"}])
    monkeypatch.setattr(plugin, "_get_all_channels", lambda logger: [{"id": 9, "name": "CNN [HD]"}])
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: [])
    monkeypatch.setattr(plugin, "_bulk_update_channels", lambda payload, fields, logger: len(payload))
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    res = plugin.restore_channels_action({"video_format_suffixes": "UHD, FHD, HD, SD, Unknown"}, _logger())
    assert res["status"] == "ok"
    assert res["restored"] == 0


# ---- Task 5: delete hygiene ---------------------------------------------
#
# The webhook tests that lived here were removed with the webhook feature on
# 2026-08-05. Notification is moving to the Newsflasharr integration.


def test_delete_prunes_restore_state(plugin, tmp_path, monkeypatch):
    plugin.channel_state_file = str(tmp_path / "state.json")
    with open(plugin.channel_state_file, "w") as f:
        json.dump({"2": {"original_group_id": 5}, "3": {"original_group_id": 6}}, f)
    _write(plugin, [{"channel_id": 2, "channel_name": "B", "status": "Dead", "error_type": "Timeout"}])
    with open(plugin.loaded_channels_file, "w") as f:
        json.dump([{"id": 2}], f)
    import iptv_checker.plugin as pm
    monkeypatch.setattr(pm.Channel.objects, "filter",
                        lambda **k: type("Q", (), {"delete": lambda self: (1, {})})(), raising=False)
    monkeypatch.setattr(plugin, "_trigger_frontend_refresh", lambda *a, **k: True)
    res = plugin.delete_dead_channels_action({"auto_delete_confirmation": "DELETE"}, _logger())
    assert res["status"] == "ok"
    state = json.load(open(plugin.channel_state_file))
    assert "2" not in state and "3" in state  # only deleted id pruned
