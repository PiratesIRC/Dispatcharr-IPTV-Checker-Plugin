"""Action button colour has to mean one thing.

Before this was pinned, colour ran opposite to consequence. Measured on
2026-09-05: the only genuinely irreversible action, Delete Dead Channels, was
red, but so were eight actions that merely rename or move a channel and remove
nothing. Clear CSV Exports was red while deleting only this plugin's own export
files. Restore Recovered Channels, which renames and moves channels exactly as
those eight do, was green, the colour used for reading. So the undo looked safer
than the do.

The scheme:

    red     can REMOVE something the operator cares about, or take a channel
            off air
    orange  writes data or clears state, but removes nothing
    green   runs a normal operation that writes no channel data
    cyan    sends something outward, to an inbox or an issue tracker
    blue    reads and reports, changing nothing

Renaming and moving are orange rather than red on purpose. Both are undone by
Restore Recovered Channels, which strips the tags this plugin added and returns
the channel to the group it came from, so neither can lose a channel.
Palette membership, action routing and label dashes are already asserted by
tests/test_email_report_action.py and tests/test_interface_copy.py, so this file
covers only what colour each action carries and why.
"""

import io
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"

# Parsed once. _actions() used to re-read the file on every call, and several
# tests call it per parametrized case.
_ACTIONS = json.loads(io.open(_MANIFEST, encoding="utf-8").read())["actions"]


def _actions():
    return _ACTIONS


_BY_ID = {a["id"]: a for a in _ACTIONS}


EXPECTED_COLOURS = {
    # red: removes channels permanently
    "delete_dead_channels": "red",

    # orange: changes channels or clears state, removes nothing
    "rename_channels": "orange",
    "rename_black_screen_channels": "orange",
    "rename_low_framerate_channels": "orange",
    "add_video_format_suffix": "orange",
    "move_dead_channels": "orange",
    "move_black_screen_channels": "orange",
    "move_low_framerate_channels": "orange",
    "restore_channels": "orange",
    "reset_progress": "orange",
    "cancel_check": "orange",
    "cleanup_orphaned_tasks": "orange",
    "clear_csv_exports": "orange",

    # green: runs an operation, writes no channel data
    "update_schedule": "green",
    "load_groups": "green",
    "check_streams": "green",
    "export_results": "green",

    # cyan: sends something outward
    "email_report": "cyan",

    # blue: reads and reports
    "validate_settings": "blue",
    "check_scheduler_status": "blue",
    "view_progress": "blue",
    "view_results": "blue",
    "view_table": "blue",
}


def test_the_expected_map_covers_every_action():
    """So an action added later cannot escape the colour rules unnoticed."""
    assert sorted(a["id"] for a in _actions()) == sorted(EXPECTED_COLOURS)


@pytest.mark.parametrize("action_id,colour", sorted(EXPECTED_COLOURS.items()))
def test_the_action_carries_the_colour_its_consequence_calls_for(action_id, colour):
    assert _BY_ID[action_id].get("button_color") == colour


def test_red_is_reserved_for_actions_that_can_remove_something():
    """If red spreads to merely noisy actions it stops carrying any warning."""
    red = sorted(a["id"] for a in _actions() if a.get("button_color") == "red")
    assert red == ["delete_dead_channels"]


def test_clearing_export_files_is_not_dressed_as_channel_deletion():
    """It removes this plugin's own CSV files and no channel data."""
    assert _BY_ID["clear_csv_exports"].get("button_color") != "red"


def test_the_undo_is_not_gentler_looking_than_the_action_it_undoes():
    by_id = {a["id"]: a.get("button_color") for a in _actions()}
    assert by_id["restore_channels"] == by_id["rename_channels"], (
        "Restore renames and moves channels exactly as the rename actions do")


def test_every_action_that_can_remove_something_asks_for_confirmation():
    """Colour is the glance; the dialog is the guard."""
    for action in _actions():
        if action.get("button_color") == "red":
            assert action.get("confirm"), f"{action['id']} has no confirmation"
