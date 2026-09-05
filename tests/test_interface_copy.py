"""House rules for every piece of text the plugin shows in the interface.

The dash rules are not cosmetic. This workspace forbids an em dash in plugin
facing copy, and a dash outside ASCII arrives as mojibake wherever the text is
rendered under a different codepage. An invisible character is worse: it cannot
be seen in review and a formatter can delete it silently.

Written after the outbound writing gate found one em dash inside a settings help
text, where reading the form would never have shown it.
"""

import io
import json
import pathlib
import unicodedata

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"

_DATA = json.loads(io.open(_MANIFEST, encoding="utf-8").read())

EM, EN = chr(0x2014), chr(0x2013)
INVISIBLE = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x2028, 0x2029, 0x00A0}


def _texts():
    """Every string a person reads, tagged so a failure names its source."""
    out = []
    for f in _DATA["fields"]:
        for key in ("label", "help_text", "description", "placeholder"):
            if f.get(key):
                out.append((f"field {f['id']}.{key}", f[key]))
    for a in _DATA["actions"]:
        for key in ("label", "button_label", "description"):
            if a.get(key):
                out.append((f"action {a['id']}.{key}", a[key]))
        confirm = a.get("confirm")
        if isinstance(confirm, dict) and confirm.get("message"):
            out.append((f"action {a['id']}.confirm", confirm["message"]))
    return out


_TEXTS = _texts()
_IDS = [name for name, _ in _TEXTS]


@pytest.mark.parametrize("name,text", _TEXTS, ids=_IDS)
def test_no_interface_text_uses_an_em_or_en_dash(name, text):
    assert EM not in text, f"{name} contains an em dash"
    assert EN not in text, f"{name} contains an en dash"


@pytest.mark.parametrize("name,text", _TEXTS, ids=_IDS)
def test_no_interface_text_carries_an_invisible_character(name, text):
    found = sorted({hex(ord(c)) for c in text if ord(c) in INVISIBLE})
    assert not found, f"{name} carries invisible characters {found}"


@pytest.mark.parametrize("name,text", _TEXTS, ids=_IDS)
def test_no_interface_text_uses_a_double_hyphen(name, text):
    """It reads as an em dash wherever the text is rendered."""
    stripped = text
    for flag in ("--show_streams", "--show_packets", "--loglevel", "--show_frames"):
        stripped = stripped.replace(flag, "")
    assert "--" not in stripped, f"{name} uses a double hyphen"


@pytest.mark.parametrize("name,text", _TEXTS, ids=_IDS)
def test_every_non_ascii_character_is_an_emoji_not_punctuation(name, text):
    """Emoji in a label are house style. Smart punctuation is not."""
    bad = []
    for ch in text:
        if ord(ch) < 128:
            continue
        category = unicodedata.category(ch)
        if category.startswith("P") or category == "Zs":
            bad.append(hex(ord(ch)))
    assert not bad, f"{name} carries non-ASCII punctuation {sorted(set(bad))}"


def test_every_stored_setting_explains_itself():
    for f in _DATA["fields"]:
        if f.get("type") == "info":
            continue
        assert f.get("help_text"), f"{f['id']} has no help text"


def test_no_interface_text_advertises_the_removed_webhook_feature():
    """The webhook was removed on 2026-08-05 and the code carries none.

    A heading still offered "webhook actions", so the form advertised something
    the plugin cannot do.
    """
    for name, text in _TEXTS:
        assert "webhook" not in text.lower(), f"{name} still offers a webhook"


# --- copy that governs destructive actions must be true ----------------------

def _section(section_id):
    f = next(x for x in _DATA["fields"] if x["id"] == section_id)
    return f.get("description") or f.get("help_text") or ""


def test_the_group_panel_states_the_exclude_mode_danger_correctly():
    """A typo in exclude mode WIDENS the run; it does not narrow it.

    Measured: _select_groups("*PVP*", "exclude", groups) returns every group,
    so a mistyped exclusion leaves the groups it meant to skip in scope and
    eligible for the scheduled rename, move and delete.
    """
    body = _section("_section_scope").lower()
    assert "exclude" in body, "the panel does not mention exclude mode at all"
    assert "narrows" not in body, (
        "the panel claims a typo narrows the run, which is false in exclude mode")


def test_the_reversible_section_names_the_change_restore_cannot_undo():
    """Restore needs captured state or a status tag. A quality suffix is neither.

    add_video_format_suffix_action never calls _capture_original_state, so a
    suffix it appends is not removed by Restore Recovered Channels.
    """
    body = _section("_section_auto_rename_move").lower()
    assert "suffix" in body, (
        "the panel promises everything here is reversible without naming the "
        "format suffix, which Restore does not strip")


def test_the_auto_run_section_does_not_claim_a_closing_window_sends_no_report():
    """A window closing mid-list DOES email a report.

    In _execute_scheduled_check the stop-event return sits at line 1894, the
    email at 1908, and the mid-list deferral at 1929. Only a session stopped
    from outside skips the report.
    """
    body = _section("_section_auto_run").lower()
    if "no report" in body or "sends no report" in body:
        assert "plugins page" in body or "stopped" in body, (
            "the claim must name the case it applies to, or a reader will apply "
            "it to an ordinary partial window, which does send a report")


def test_the_post_check_panel_counts_its_sub_sections_correctly():
    ids = [f["id"] for f in _DATA["fields"] if f.get("type") == "info"]
    between = ids[ids.index("_section_post_check") + 1:ids.index("_section_scheduling")]
    body = _section("_section_post_check").lower()
    words = {4: "four", 5: "five", 6: "six"}
    assert words[len(between)] in body, (
        f"there are {len(between)} sections below it, and the body says otherwise")


@pytest.mark.parametrize("action", [
    a for a in _DATA["actions"] if a.get("button_color") == "orange"
], ids=[a["id"] for a in _DATA["actions"] if a.get("button_color") == "orange"])
def test_an_orange_action_does_not_claim_to_be_irreversible(action):
    """Colour says reversible and the dialog said the opposite, in one change."""
    confirm = action.get("confirm") or {}
    message = (confirm.get("message") or "") if isinstance(confirm, dict) else ""
    assert "irreversible" not in message.lower(), (
        f"{action['id']} is orange because it can be undone, but its dialog says "
        "the action is irreversible")


def test_the_export_clearing_dialog_does_not_overstate_its_scope():
    """It deletes only iptv_checker_results_*.csv from a shared directory."""
    action = next(a for a in _DATA["actions"] if a["id"] == "clear_csv_exports")
    message = (action.get("confirm") or {}).get("message", "")
    assert "all CSV files" not in message, (
        "the directory is shared with other plugins and their files are not touched")
