"""Pure, Django-free helper coverage for plugin.py.

These are the stdlib-only helpers that have no ORM / network / filesystem
coupling beyond a tmp file: cron parsing & matching, Streamlink-host URL
classification, and the atomic JSON read/write pair. Expected values are read
straight off the function bodies in plugin.py, not guessed.
"""
from datetime import datetime


# --- _parse_scheduled_times ---------------------------------------------------

def test_parse_scheduled_times_empty_returns_empty(plugin):
    assert plugin._parse_scheduled_times("") == []
    assert plugin._parse_scheduled_times("   ") == []
    assert plugin._parse_scheduled_times(None) == []


def test_parse_scheduled_times_keeps_valid_five_field_exprs(plugin):
    assert plugin._parse_scheduled_times("0 4 * * *") == ["0 4 * * *"]


def test_parse_scheduled_times_splits_and_trims_comma_list(plugin):
    assert plugin._parse_scheduled_times("0 4 * * * , 0 3 1 * *") == [
        "0 4 * * *",
        "0 3 1 * *",
    ]


def test_parse_scheduled_times_drops_wrong_field_count(plugin):
    # 4 fields and 6 fields are both invalid; only the valid 5-field survives.
    assert plugin._parse_scheduled_times("0 4 * *, 0 4 * * *, 0 4 * * * *") == [
        "0 4 * * *",
    ]


# --- _cron_field_matches ------------------------------------------------------

def test_cron_field_wildcard_matches_anything(plugin):
    assert plugin._cron_field_matches("*", 0, 0, 59) is True
    assert plugin._cron_field_matches("*", 59, 0, 59) is True


def test_cron_field_step_values(plugin):
    # */2 matches even values (current_value % step == 0)
    assert plugin._cron_field_matches("*/2", 4, 0, 23) is True
    assert plugin._cron_field_matches("*/2", 3, 0, 23) is False


def test_cron_field_lists(plugin):
    assert plugin._cron_field_matches("1,3,5", 3, 0, 6) is True
    assert plugin._cron_field_matches("1,3,5", 2, 0, 6) is False


def test_cron_field_ranges(plugin):
    assert plugin._cron_field_matches("1-5", 5, 0, 6) is True
    assert plugin._cron_field_matches("1-5", 6, 0, 6) is False


def test_cron_field_specific_value(plugin):
    assert plugin._cron_field_matches("7", 7, 0, 23) is True
    assert plugin._cron_field_matches("7", 8, 0, 23) is False


def test_cron_field_garbage_is_false(plugin):
    assert plugin._cron_field_matches("abc", 1, 0, 23) is False
    assert plugin._cron_field_matches("*/x", 1, 0, 23) is False


# --- _cron_matches ------------------------------------------------------------

def test_cron_matches_daily_at_specific_minute_hour(plugin):
    # "0 4 * * *" = every day at 04:00
    assert plugin._cron_matches("0 4 * * *", datetime(2026, 6, 10, 4, 0)) is True
    assert plugin._cron_matches("0 4 * * *", datetime(2026, 6, 10, 4, 1)) is False
    assert plugin._cron_matches("0 4 * * *", datetime(2026, 6, 10, 5, 0)) is False


def test_cron_matches_day_of_month(plugin):
    # "0 3 1 * *" = 1st of month at 03:00
    assert plugin._cron_matches("0 3 1 * *", datetime(2026, 6, 1, 3, 0)) is True
    assert plugin._cron_matches("0 3 1 * *", datetime(2026, 6, 2, 3, 0)) is False


def test_cron_matches_weekday_uses_cron_sunday_zero(plugin):
    # plugin converts python weekday (Mon=0) to cron weekday via (wd + 1) % 7,
    # so cron Sunday=0. Verify the conversion against a concrete date.
    dt = datetime(2026, 6, 7, 3, 0)  # whatever weekday this is
    cron_wd = (dt.weekday() + 1) % 7
    assert plugin._cron_matches(f"0 3 * * {cron_wd}", dt) is True
    assert plugin._cron_matches(f"0 3 * * {(cron_wd + 1) % 7}", dt) is False


def test_cron_matches_rejects_malformed_expression(plugin):
    assert plugin._cron_matches("0 4 * *", datetime(2026, 6, 10, 4, 0)) is False
    assert plugin._cron_matches("garbage", datetime(2026, 6, 10, 4, 0)) is False


def test_cron_matches_step_minute(plugin):
    # "*/15 * * * *" matches minutes divisible by 15
    assert plugin._cron_matches("*/15 * * * *", datetime(2026, 6, 10, 9, 30)) is True
    assert plugin._cron_matches("*/15 * * * *", datetime(2026, 6, 10, 9, 31)) is False


# --- _streamlink_host_suffixes ------------------------------------------------

def test_streamlink_suffixes_default_when_blank(plugin):
    assert plugin._streamlink_host_suffixes({}) == [
        "youtube.com", "youtu.be", "twitch.tv", "kick.com",
    ]
    assert plugin._streamlink_host_suffixes(None) == [
        "youtube.com", "youtu.be", "twitch.tv", "kick.com",
    ]


def test_streamlink_suffixes_custom_are_normalized(plugin):
    # lower-cased, trimmed, leading dots stripped, blanks dropped
    out = plugin._streamlink_host_suffixes({"streamlink_hosts": " .Foo.COM , bar.tv ,, "})
    assert out == ["foo.com", "bar.tv"]


# --- _is_streamlink_only_url --------------------------------------------------

def test_streamlink_url_exact_host_match(plugin):
    assert plugin._is_streamlink_only_url("https://twitch.tv/somechannel") is True


def test_streamlink_url_subdomain_match(plugin):
    assert plugin._is_streamlink_only_url("https://www.youtube.com/watch?v=abc") is True


def test_streamlink_url_non_matching_host(plugin):
    assert plugin._is_streamlink_only_url("http://example.com/live/1.ts") is False


def test_streamlink_url_lookalike_suffix_not_matched(plugin):
    # endswith('.twitch.tv') guards against "nottwitch.tv" but a plain
    # "faketwitch.tv" must not match the exact-or-dotted-suffix rule.
    assert plugin._is_streamlink_only_url("http://faketwitch.tv/x") is False


def test_streamlink_url_empty_is_false(plugin):
    assert plugin._is_streamlink_only_url("") is False
    assert plugin._is_streamlink_only_url(None) is False


# --- _save_json_file / _load_json_file (atomic round-trip) --------------------

def test_json_round_trip(plugin, tmp_path):
    path = str(tmp_path / "round.json")
    data = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    plugin._save_json_file(path, data)
    assert plugin._load_json_file(path) == data


def test_load_missing_file_returns_none(plugin, tmp_path):
    assert plugin._load_json_file(str(tmp_path / "nope.json")) is None


def test_load_corrupted_file_returns_none(plugin, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert plugin._load_json_file(str(path)) is None


def test_save_leaves_no_tmp_file_behind(plugin, tmp_path):
    path = tmp_path / "clean.json"
    plugin._save_json_file(str(path), {"ok": 1})
    assert path.exists()
    assert not (tmp_path / "clean.json.tmp").exists()


def test_save_serializes_non_native_via_default_str(plugin, tmp_path):
    # _save_json_file passes default=str, so a datetime is stringified, not raised.
    path = str(tmp_path / "dt.json")
    plugin._save_json_file(path, {"when": datetime(2026, 6, 10, 4, 0)})
    loaded = plugin._load_json_file(path)
    assert loaded["when"] == "2026-06-10 04:00:00"


# --- _match_group_names (group include/exclude matcher) ---

def test_match_group_names_exact_and_wildcard(pmod):
    P = pmod.Plugin
    groups = {"US Sports", "US-PPV-1", "US-PPV-2", "Movies", "Graveyard"}
    assert P._match_group_names("Movies", groups) == {"Movies"}
    assert P._match_group_names("US-PPV-*", groups) == {"US-PPV-1", "US-PPV-2"}
    assert P._match_group_names("Movies, US-PPV-*", groups) == {"Movies", "US-PPV-1", "US-PPV-2"}


def test_match_group_names_no_match_and_blank(pmod):
    P = pmod.Plugin
    groups = {"Movies", "Graveyard"}
    assert P._match_group_names("Nope", groups) == set()
    assert P._match_group_names("", groups) == set()
    assert P._match_group_names("  ,  ", groups) == set()


def test_match_group_names_case_sensitive(pmod):
    P = pmod.Plugin
    groups = {"Graveyard"}
    assert P._match_group_names("graveyard", groups) == set()   # exact is case-sensitive
    assert P._match_group_names("grave*", groups) == set()      # fnmatchcase is case-sensitive
    assert P._match_group_names("Grave*", groups) == {"Graveyard"}


# --- _parse_scheduled_times: comma is legal INSIDE a cron field ----------------
# GitHub issue 27. The setting holds several cron expressions, and the
# documented separator was a comma. A comma is also legal inside a single cron
# field, where it means a list of values, so "0 0,8,16 * * *" (one valid
# expression meaning midnight, 08:00 and 16:00) was chopped into three
# fragments and the whole schedule was rejected. Worse, a string mixing both
# uses returned a PARTIAL schedule with no error, so the action reported
# success while saving times the operator never asked for.

def test_parse_scheduled_times_keeps_a_value_list_inside_one_field(plugin):
    assert plugin._parse_scheduled_times("0 0,8,16 * * *") == ["0 0,8,16 * * *"]


def test_parse_scheduled_times_splits_on_semicolon(plugin):
    assert plugin._parse_scheduled_times("0 0,8 * * * ; 30 2 * * *") == [
        "0 0,8 * * *",
        "30 2 * * *",
    ]


def test_parse_scheduled_times_splits_on_newline(plugin):
    assert plugin._parse_scheduled_times("0 0,8 * * *\n30 2 * * *") == [
        "0 0,8 * * *",
        "30 2 * * *",
    ]

def test_value_list_schedule_fires_at_each_listed_hour(plugin):
    # End to end over the pair the operator actually depends on: the string is
    # parsed into one expression, and that expression matches at each of the
    # three listed hours and at no other hour.
    from datetime import datetime

    exprs = plugin._parse_scheduled_times("0 0,8,16 * * *")
    assert len(exprs) == 1
    fires = [h for h in range(24)
             if plugin._cron_matches(exprs[0], datetime(2026, 8, 28, h, 0))]
    assert fires == [0, 8, 16]

