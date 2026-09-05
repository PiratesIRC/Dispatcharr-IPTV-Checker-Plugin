"""A setting that is off must be read as off, whatever shape it is stored in.

Every boolean read site used plain truthiness, so a value stored as the string
"false" was read as ON. Nine of those sites gate a scheduled rename, move or
delete, so the failure would arm a destructive action the operator had switched
off, and the CSV record would have said No beside it because the report renders
the human reading.

Measured on this installation Dispatcharr stores these as real booleans, so the
string shapes are defensive. The report and the behaviour still have to agree
whatever arrives, which is why _yes_no is defined in terms of _as_bool rather
than parsing separately.
"""

import io
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "iptv_checker" / "plugin.py"
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"

_BOOLEAN_FIELDS = sorted(
    f["id"] for f in json.loads(io.open(_MANIFEST, encoding="utf-8").read())["fields"]
    if f.get("type") == "boolean"
)

SHAPES = [
    (True, True), (False, False),
    ("true", True), ("True", True), ("TRUE", True), (" true ", True),
    ("yes", True), ("on", True), ("1", True), (1, True),
    ("false", False), ("False", False), ("no", False), ("off", False),
    ("0", False), (0, False), ("", False),
]


@pytest.mark.parametrize("stored,expected", SHAPES)
def test_as_bool_reads_every_shape_a_setting_can_arrive_in(pmod, stored, expected):
    assert pmod._as_bool(stored) is expected


def test_as_bool_falls_back_to_the_default_when_unset(pmod):
    assert pmod._as_bool(None, True) is True
    assert pmod._as_bool(None, False) is False


@pytest.mark.parametrize("stored,expected", SHAPES)
def test_the_report_and_the_behaviour_cannot_disagree(pmod, stored, expected):
    """The whole point: one parser, so No in the record means off in the code."""
    assert pmod._yes_no(stored) == ("Yes" if expected else "No")
    assert pmod._as_bool(stored) is expected


@pytest.mark.parametrize("field_id", _BOOLEAN_FIELDS)
def test_every_boolean_setting_is_read_through_the_shared_parser(field_id):
    """Plain truthiness reads the string "false" as on."""
    offenders = []
    for number, line in enumerate(io.open(_SOURCE, encoding="utf-8").read().split("\n"), 1):
        quoted = (f"'{field_id}'" in line) or (f'"{field_id}"' in line)
        if not quoted or ".get(" not in line:
            continue
        if "_as_bool" in line or "_yes_no" in line:
            continue
        offenders.append(f"line {number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_a_destructive_action_stays_off_when_stored_as_the_string_false(pmod):
    """The case that matters: a switched-off delete must not run."""
    assert pmod._as_bool("false") is False
    assert pmod._as_bool("false", True) is False, (
        "a stored value must beat the default, or turning something off does nothing")
