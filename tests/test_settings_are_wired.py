"""Every setting on the form must reach the code that would honour it.

A control that does nothing is worse than a missing one: it tells the operator
they have made a choice. Measured on 2026-09-05, scheduler_export_csv had a
label, help text and a default of off, and appeared nowhere in plugin.py. The
scheduled CSV export is unconditional by design, because the file is the record
of what was probed, so the toggle promised the opposite of the behaviour in both
directions.
"""

import ast
import io
import json
import re
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "iptv_checker" / "plugin.py"
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"


_MANIFEST_DATA = json.loads(io.open(_MANIFEST, encoding="utf-8").read())


def _manifest():
    return _MANIFEST_DATA


def _stored_field_ids():
    """Every field that holds a value. Section headings store nothing."""
    return [f["id"] for f in _manifest()["fields"] if f.get("type") != "info"]


def _string_literals():
    """Every string constant in the source, from the parsed tree.

    A plain substring search over the file is the shape this project records as
    insufficient: it is satisfied by a mention in a comment or in dead code, and
    renaming a read to scheduler_email_report_DISABLED still contains the
    original id. Parsing drops comments entirely and compares whole literals, so
    neither hole survives.

    Docstrings are string constants too, so a field id mentioned only in prose
    would satisfy this. That is a known limit, not a claim to have closed it.
    """
    tree = ast.parse(io.open(_SOURCE, encoding="utf-8").read())
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


_LITERALS = _string_literals()


@pytest.mark.parametrize("field_id", _stored_field_ids())
def test_every_setting_is_read_somewhere_in_the_plugin(field_id):
    assert field_id in _LITERALS, (
        f"the settings form offers {field_id!r} and no string literal in the code "
        "matches it, so the control does nothing"
    )


def test_the_dead_scheduled_export_toggle_is_gone():
    """It could not work: the scheduled export is deliberately unconditional."""
    assert "scheduler_export_csv" not in _stored_field_ids()


def test_the_scheduled_export_stays_unconditional():
    """Removing the toggle must not be mistaken for making it configurable."""
    source = io.open(_SOURCE, encoding="utf-8").read()
    assert "scheduler_export_csv" not in source


def test_the_user_guide_does_not_describe_the_removed_toggle():
    """Removing a setting without updating the guide creates a false document."""
    guide = _ROOT / "docs" / "USER-GUIDE.md"
    if not guide.exists():
        pytest.skip("no user guide in this checkout")
    text = io.open(guide, encoding="utf-8").read()
    assert "| Export CSV |" not in text, (
        "the guide still lists a setting that no longer exists")


def test_the_user_guide_covers_the_export_retention_setting():
    guide = _ROOT / "docs" / "USER-GUIDE.md"
    if not guide.exists():
        pytest.skip("no user guide in this checkout")
    text = io.open(guide, encoding="utf-8").read()
    assert "Delete CSV Exports Older" in text


def test_no_setting_is_read_that_the_form_never_offers():
    """The reverse direction, which is the one that shadows a stale value.

    Dispatcharr never prunes a stored setting, so a read of a key that was
    removed from the form keeps returning whatever was last saved. Currently
    nothing does this, and the assertion is cheap enough to keep it that way.
    """
    source = io.open(_SOURCE, encoding="utf-8").read()
    read = set(re.findall(r"settings\.get\(\s*['\"]([^'\"]+)['\"]", source))
    declared = set(_stored_field_ids())
    # Keys that are plugin state rather than form settings live here.
    internal = {"scheduled", "logger", "group_names", "group_names_exclude"}
    unknown = sorted(read - declared - internal)
    assert unknown == [], f"read but never offered on the form: {unknown}"
