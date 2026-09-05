"""The section headings that group the settings form.

A heading is an ordinary field of type "info" with an id prefixed _section_. It
stores nothing, so it carries no "default" key: Dispatcharr never prunes a
stored setting, and a heading that stored a value would leave one behind for
good.

These tests pin the BOUNDARY of each section, meaning which setting opens it,
rather than the full membership. Adding a setting inside a section then needs no
test change, while moving a boundary does.

Two headings deliberately hold no settings of their own. Post-Check Actions
introduces the five sub-sections beneath it, and Restore Recovered Channels
explains a feature whose only automatic control lives with the other scheduled
toggles. Both are asserted below so that neither reads as a mistake.
"""

import io
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"


# Parsed once. This used to re-read the file on every call, and _members()
# calls it per section inside a comprehension over all of them.
_FIELDS = json.loads(io.open(_MANIFEST, encoding="utf-8").read())["fields"]


def _fields():
    return _FIELDS


def _ids():
    return [f["id"] for f in _fields()]


def _headings():
    return [f for f in _fields() if f.get("type") == "info"]


def _members(section_id):
    """The settings between this heading and the next one."""
    out, seen = [], False
    for f in _fields():
        if f.get("type") == "info":
            if seen:
                break
            seen = f["id"] == section_id
        elif seen:
            out.append(f["id"])
    return out


EXPECTED_ORDER = [
    "_section_scope",
    "_section_check_behavior",
    "_section_black_screen",
    "_section_placeholder_file",
    "_section_frozen_video",
    "_section_silent_audio",
    "_section_post_check",
    "_section_dead",
    "_section_black",
    "_section_low_fps",
    "_section_format",
    "_section_restore",
    "_section_scheduling",
    "_section_auto_run",
    "_section_auto_rename_move",
    "_section_auto_delete",
    "_section_advanced",
]

# Which setting opens each section. The two explainers open nothing.
SECTION_BOUNDARIES = [
    ("_section_scope", "channel_groups"),
    ("_section_check_behavior", "timeout"),
    ("_section_black_screen", "black_screen_detection"),
    ("_section_placeholder_file", "placeholder_file_detection"),
    ("_section_frozen_video", "frozen_video_detection"),
    ("_section_silent_audio", "silent_audio_detection"),
    ("_section_dead", "dead_rename_format"),
    ("_section_black", "black_screen_rename_format"),
    ("_section_low_fps", "low_framerate_rename_format"),
    ("_section_format", "video_format_suffixes"),
    ("_section_scheduling", "scheduled_times"),
    ("_section_auto_run", "scheduler_email_report"),
    ("_section_auto_rename_move", "scheduler_restore_channels"),
    ("_section_auto_delete", "scheduler_delete_dead_channels"),
    ("_section_advanced", "ffprobe_flags"),
]

EXPLAINERS_WITH_NO_SETTINGS = ["_section_post_check", "_section_restore"]


def test_every_expected_section_heading_is_present():
    present = {f["id"] for f in _headings()}
    assert set(EXPECTED_ORDER) <= present


def test_the_sections_appear_in_the_expected_order():
    assert [f["id"] for f in _headings()] == EXPECTED_ORDER


@pytest.mark.parametrize("section_id,opener", SECTION_BOUNDARIES,
                         ids=[s for s, _ in SECTION_BOUNDARIES])
def test_each_section_is_followed_by_the_setting_that_opens_it(section_id, opener):
    ids = _ids()
    assert ids[ids.index(section_id) + 1] == opener


@pytest.mark.parametrize("section_id", EXPLAINERS_WITH_NO_SETTINGS)
def test_an_explainer_heading_holds_no_settings_of_its_own(section_id):
    """Stated so that a heading with nothing under it does not read as a bug."""
    assert _members(section_id) == []


def test_no_setting_sits_above_the_first_heading():
    fields = _fields()
    assert fields[0].get("type") == "info", (
        f"{fields[0]['id']} sits above every heading and belongs to no section")


def test_no_section_holds_more_settings_than_a_reader_can_scan():
    """One section held twelve, which read as a single undifferentiated list."""
    oversized = {s: len(_members(s)) for s in EXPECTED_ORDER if len(_members(s)) > 8}
    assert oversized == {}


def test_the_irreversible_action_has_its_own_section():
    """Deleting channels is the only automatic action that cannot be undone."""
    members = _members("_section_auto_delete")
    assert "scheduler_delete_dead_channels" in members
    assert "auto_delete_confirmation" in members


def test_the_reversible_channel_changes_sit_together():
    members = _members("_section_auto_rename_move")
    assert "scheduler_delete_dead_channels" not in members, (
        "deleting is not reversible and must not sit with the changes that are")
    for expected in ("scheduler_restore_channels", "scheduler_rename_dead_channels",
                     "scheduler_move_dead_channels"):
        assert expected in members


# --- how a heading must be written ------------------------------------------

@pytest.mark.parametrize("field", _headings(), ids=[f["id"] for f in _headings()])
def test_a_heading_stores_nothing(field):
    assert field.get("type") == "info"
    assert "default" not in field, (
        "Dispatcharr never prunes a stored setting, so a heading that stored one "
        "would leave a value behind for good")


@pytest.mark.parametrize("field", _headings(), ids=[f["id"] for f in _headings()])
def test_a_heading_body_is_one_flowing_paragraph(field):
    """Line breaks are not safe in an info panel body."""
    body = field.get("help_text") or field.get("description") or ""
    assert "\n" not in body


@pytest.mark.parametrize("field", _headings(), ids=[f["id"] for f in _headings()])
def test_a_heading_uses_no_em_dash(field):
    text = (field.get("label") or "") + (field.get("help_text") or "") + \
           (field.get("description") or "")
    assert chr(0x2014) not in text
    assert chr(0x2013) not in text


@pytest.mark.parametrize("field", _headings(), ids=[f["id"] for f in _headings()])
def test_a_heading_says_something_beyond_its_title(field):
    body = field.get("help_text") or field.get("description") or ""
    assert len(body.split()) >= 12, "a heading with no body explains nothing"
