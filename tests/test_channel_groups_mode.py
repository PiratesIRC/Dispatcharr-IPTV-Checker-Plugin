"""Channel Groups + Channel Groups Mode, replacing the old include/exclude pair.

The two settings `group_names` ("Group(s) to Check") and `group_names_exclude`
("Group(s) to EXCLUDE") are replaced by one list plus a mode, matching the shape
Stream-Mapparr uses:

    channel_groups       comma-separated list, wildcards supported
    channel_groups_mode  include | exclude

TWO DELIBERATE DIFFERENCES FROM STREAM-MAPPARR, both measured rather than assumed:

1. WILDCARDS ARE KEPT. Stream-Mapparr resolves group names to ids and matches
   them exactly; this plugin matches with fnmatch. Copying Stream-Mapparr
   literally would silently stop a stored value like `*PPV*` from matching
   anything, which on a live install means groups that were being skipped
   quietly start being checked and acted on.

2. MIGRATION FROM THE OLD PAIR. Dispatcharr never prunes a stored setting when
   its field is removed, so the old values survive in the database forever. They
   are read as a fallback so an upgrade does not silently widen the scope of a
   destructive action.

WHAT IS LOST, stated plainly: the old pair could apply an include list AND an
exclude list at the same time. One list plus a mode cannot express that. The
migration path below keeps both applied for installs that were using both, so
no existing configuration changes behaviour on upgrade, but a NEW configuration
can only be one or the other.
"""


# ---- mode resolution -----------------------------------------------------

def test_explicit_include_mode(pmod):
    s = {"channel_groups": "Sports, News", "channel_groups_mode": "include"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("Sports, News", "include", "")


def test_explicit_exclude_mode(pmod):
    s = {"channel_groups": "*PPV*", "channel_groups_mode": "exclude"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("*PPV*", "exclude", "")


def test_unknown_mode_falls_back_to_include(pmod):
    """Include is the pre-existing meaning of a group list. A stored value this
    build does not understand must keep a list of WANTED groups meaning wanted,
    rather than inverting it into a list of skipped ones."""
    for bad in ("nonsense", "", None, 5, True, [], {"a": 1}):
        patterns, mode, legacy = pmod.Plugin._resolve_channel_groups(
            {"channel_groups": "Sports", "channel_groups_mode": bad})
        assert mode == "include", f"mode {bad!r} should resolve to include"


def test_mode_is_case_and_space_insensitive(pmod):
    for value in ("EXCLUDE", " exclude ", "Exclude"):
        _, mode, _ = pmod.Plugin._resolve_channel_groups(
            {"channel_groups": "x", "channel_groups_mode": value})
        assert mode == "exclude", f"{value!r} should resolve to exclude"


def test_empty_list_and_no_legacy_means_all_groups(pmod):
    for mode in ("include", "exclude"):
        patterns, resolved, legacy = pmod.Plugin._resolve_channel_groups(
            {"channel_groups": "", "channel_groups_mode": mode})
        assert patterns == ""
        assert legacy == ""


def test_missing_settings_are_safe(pmod):
    assert pmod.Plugin._resolve_channel_groups({}) == ("", "include", "")
    assert pmod.Plugin._resolve_channel_groups(None) == ("", "include", "")


# ---- migration from the old include/exclude pair -------------------------

def test_legacy_include_only_migrates_to_include_mode(pmod):
    s = {"channel_groups": "", "group_names": "US-*", "group_names_exclude": ""}
    assert pmod.Plugin._resolve_channel_groups(s) == ("US-*", "include", "")


def test_legacy_exclude_only_migrates_to_exclude_mode(pmod):
    """This is the live configuration on the operator's box: an empty include
    list and an exclude list of `*PPV*`, meaning every group except PPV. It must
    keep meaning exactly that after the upgrade."""
    s = {"channel_groups": "", "group_names": "", "group_names_exclude": "*PPV*"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("*PPV*", "exclude", "")


def test_legacy_both_set_keeps_both_applied(pmod):
    """One list plus a mode cannot express include-minus-exclude, so the old
    exclude list is returned separately and still applied. Without this, an
    upgrade would WIDEN the scope: the previously excluded groups would start
    being checked, and any destructive post-action would start acting on them."""
    s = {"channel_groups": "", "group_names": "US-*", "group_names_exclude": "US-PPV-*"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("US-*", "include", "US-PPV-*")


def test_new_setting_wins_over_legacy(pmod):
    """Once anything is entered in the new box, the stale legacy values in the
    database are ignored entirely."""
    s = {"channel_groups": "Movies", "channel_groups_mode": "include",
         "group_names": "US-*", "group_names_exclude": "US-PPV-*"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("Movies", "include", "")


def test_new_setting_wins_even_when_mode_is_exclude(pmod):
    s = {"channel_groups": "Movies", "channel_groups_mode": "exclude",
         "group_names": "US-*", "group_names_exclude": "US-PPV-*"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("Movies", "exclude", "")


def test_legacy_values_that_are_only_whitespace_are_ignored(pmod):
    s = {"channel_groups": "  ", "group_names": "   ", "group_names_exclude": "  "}
    assert pmod.Plugin._resolve_channel_groups(s) == ("", "include", "")


def test_wildcard_escape_hatch_from_a_stale_legacy_value(pmod):
    """A legacy value survives in the database forever, so an operator who wants
    ALL groups while a stale legacy exclude is still stored needs a way to say
    so. An explicit `*` in include mode matches every group and stops the
    fallback, because the new box is no longer empty."""
    s = {"channel_groups": "*", "channel_groups_mode": "include",
         "group_names_exclude": "*PPV*"}
    assert pmod.Plugin._resolve_channel_groups(s) == ("*", "include", "")


# ---- the matcher itself is unchanged and still does wildcards ------------

def test_wildcards_still_work(pmod):
    """The whole reason Stream-Mapparr's exact-name matching was NOT copied."""
    groups = {"US-PPV-1", "US-PPV-2", "Movies", "News"}
    assert pmod.Plugin._match_group_names("*PPV*", groups) == {"US-PPV-1", "US-PPV-2"}


# ---- applying the mode ---------------------------------------------------

def test_apply_include_keeps_only_listed(pmod):
    groups = {"Sports", "News", "Movies"}
    assert pmod.Plugin._select_groups("Sports, News", "include", groups) == {"Sports", "News"}


def test_apply_exclude_drops_listed(pmod):
    groups = {"Sports", "News", "Movies"}
    assert pmod.Plugin._select_groups("Sports", "exclude", groups) == {"News", "Movies"}


def test_apply_exclude_with_wildcard(pmod):
    groups = {"US-PPV-1", "US-PPV-2", "Movies"}
    assert pmod.Plugin._select_groups("*PPV*", "exclude", groups) == {"Movies"}


def test_empty_list_selects_everything_in_both_modes(pmod):
    groups = {"Sports", "News"}
    assert pmod.Plugin._select_groups("", "include", groups) == groups
    assert pmod.Plugin._select_groups("", "exclude", groups) == groups


def test_include_with_no_matches_selects_nothing(pmod):
    """Distinct from an empty list. A list that matches nothing is a typo, and
    the caller reports it as an error rather than silently checking everything."""
    assert pmod.Plugin._select_groups("Nope", "include", {"Sports"}) == set()


def test_exclude_everything_selects_nothing(pmod):
    assert pmod.Plugin._select_groups("*", "exclude", {"Sports", "News"}) == set()


def test_legacy_exclude_is_applied_on_top(pmod):
    """The migration case where both old settings were set."""
    groups = {"US-1", "US-PPV-1", "Movies"}
    selected = pmod.Plugin._select_groups("US-*", "include", groups)
    assert selected == {"US-1", "US-PPV-1"}
    selected -= pmod.Plugin._match_group_names("US-PPV-*", groups)
    assert selected == {"US-1"}


# ---- the settings fingerprint must track the new keys -------------------

def test_fingerprint_changes_with_mode(pmod, plugin):
    """The fingerprint drives windowed-resume drift detection. If the mode is
    not in it, flipping include to exclude would resume a run whose scope is now
    the exact complement of what it was."""
    base = {"channel_groups": "Sports", "channel_groups_mode": "include"}
    flipped = {"channel_groups": "Sports", "channel_groups_mode": "exclude"}
    assert plugin._settings_fingerprint(base) != plugin._settings_fingerprint(flipped)


def test_fingerprint_changes_with_group_list(pmod, plugin):
    a = {"channel_groups": "Sports", "channel_groups_mode": "include"}
    b = {"channel_groups": "News", "channel_groups_mode": "include"}
    assert plugin._settings_fingerprint(a) != plugin._settings_fingerprint(b)
