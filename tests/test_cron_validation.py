"""A saved schedule must be able to fire, or be refused.

The parser used to check only that an expression had five whitespace separated
fields. It did not look at what was in them, so several inputs an operator can
reasonably type were accepted, reported as saved, and then never ran:

    0 25 * * *              hour 25 matches no hour
    "0 22 * * 0,2,4"        quotation marks become part of the fields
    0 22 * * SUN,TUE,THU    this plugin matches numbers, not day names
    0 22 * * 0,2,4,         the trailing comma leaves an empty value

and one that ran but not as asked:

    0 22 * * 0, 2, 4        the spaces split it, and only Sundays survived

A schedule that silently never runs is the fault this plugin spent 2026-09-05
on, so an expression that cannot fire is now recognised as such.

The two callers deliberately differ. SAVING refuses the whole input and names
what was wrong, because the operator is there to correct it. LOADING a stored
schedule keeps whatever still works, because refusing the lot would turn an
installation that was partly running into one that runs nothing at all,
silently, at boot. That is the worse failure and this project has had it twice.
"""

import pytest


CAN_NEVER_FIRE = [
    ("0 25 * * *", "hour above 23"),
    ("70 8 * * *", "minute above 59"),
    ("0 8 32 * *", "day of month above 31"),
    ("0 8 0 * *", "day of month zero"),
    ("0 8 * 13 *", "month above 12"),
    ("0 8 * 0 *", "month zero"),
    ("0 8 * * 8", "day of week above 7"),
    ('"0 22 * * 0,2,4"', "wrapped in quotation marks"),
    ("0 22 * * SUN,TUE,THU", "day names rather than numbers"),
    ("0 22 * * 0,2,4,", "trailing comma leaves an empty value"),
    ("0 8 * * 5-1", "reversed range matches nothing"),
    ("*/0 * * * *", "a step of zero"),
    ("0 8 * * 1-3,5", "a range inside a list, which the matcher cannot read"),
]


@pytest.mark.parametrize("expr,reason", CAN_NEVER_FIRE,
                         ids=[r for _, r in CAN_NEVER_FIRE])
def test_an_expression_that_can_never_fire_is_refused(plugin, expr, reason):
    assert plugin._parse_scheduled_times(expr) == [], reason


CAN_FIRE = [
    "0 22 * * 0,2,4",
    "0 0,8,16 * * *",
    "*/15 * * * *",
    "0 */6 * * *",
    "0 4 * * 1-5",
    "0 2 */2 * *",
    "35 8 * * *",
    "0 0 * * *",
    "59 23 31 12 6",
    "0 3 * * 7",
]


@pytest.mark.parametrize("expr", CAN_FIRE)
def test_a_usable_expression_is_still_accepted(plugin, expr):
    assert plugin._parse_scheduled_times(expr) == [expr]


def test_several_expressions_separated_by_a_semicolon(plugin):
    assert plugin._parse_scheduled_times("35 8 * * *;0 23 * * *") == [
        "35 8 * * *", "0 23 * * *"]


def test_the_legacy_comma_separator_still_works(plugin):
    """Saved schedules use a comma between whole expressions; do not break them."""
    assert plugin._parse_scheduled_times("0 4 * * *,0 16 * * *") == [
        "0 4 * * *", "0 16 * * *"]


def _save(plugin, monkeypatch, quiet_logger, text):
    monkeypatch.setattr(plugin, "_owns_scheduler_lock", lambda: False)
    monkeypatch.setattr(plugin, "_request_scheduler_reload", lambda: None)
    monkeypatch.setattr(plugin, "_dispatcharr_timezone", lambda: "America/Chicago")
    return plugin.update_schedule_action({"scheduled_times": text}, quiet_logger)


def test_spaces_after_the_commas_are_refused_when_saving(
        plugin, monkeypatch, quiet_logger):
    """This is the dangerous one: it used to save, and run on Sundays only.

    Keeping the surviving fragment stored a schedule the operator did not ask
    for and reported success, so the loss was invisible.
    """
    result = _save(plugin, monkeypatch, quiet_logger, "0 22 * * 0, 2, 4")

    assert result["status"] == "error"
    assert "cannot ever run" in result["message"]


def test_one_unusable_expression_refuses_the_whole_save(
        plugin, monkeypatch, quiet_logger):
    result = _save(plugin, monkeypatch, quiet_logger, "0 4 * * *,0 99 * * *")

    assert result["status"] == "error"


def test_a_usable_schedule_still_saves(plugin, monkeypatch, quiet_logger):
    """Control, so the refusal above is not passing for an unrelated reason."""
    result = _save(plugin, monkeypatch, quiet_logger, "0 22 * * 0,2,4")

    assert result["status"] == "ok"


def test_loading_a_stored_schedule_keeps_what_still_works(plugin):
    """The opposite rule at load time: never leave an install with no schedule."""
    assert plugin._parse_scheduled_times("0 4 * * *,0 99 * * *") == ["0 4 * * *"]


def test_a_semicolon_list_keeps_the_expressions_that_can_fire(plugin):
    """A semicolon is unambiguous, so a bad entry does not condemn the others."""
    assert plugin._parse_scheduled_times("0 4 * * *;0 99 * * *") == ["0 4 * * *"]


# --- day of week 7 means Sunday, as in standard cron ------------------------

def test_day_of_week_seven_fires_on_sunday(plugin):
    """Before this change it fired on nothing, while the interface called it Sunday."""
    from datetime import datetime

    fired = [f"{datetime(2026, 9, d):%a}" for d in range(6, 13)
             if plugin._cron_matches("30 2 * * 7", datetime(2026, 9, d, 2, 30))]
    assert fired == ["Sun"]


def test_day_of_week_zero_still_fires_on_sunday(plugin):
    from datetime import datetime

    fired = [f"{datetime(2026, 9, d):%a}" for d in range(6, 13)
             if plugin._cron_matches("30 2 * * 0", datetime(2026, 9, d, 2, 30))]
    assert fired == ["Sun"]


def test_a_list_containing_seven_fires_on_sunday(plugin):
    from datetime import datetime

    fired = [f"{datetime(2026, 9, d):%a}" for d in range(6, 13)
             if plugin._cron_matches("30 2 * * 2,7", datetime(2026, 9, d, 2, 30))]
    assert fired == ["Sun", "Tue"]


def test_seven_does_not_make_every_day_match(plugin):
    """A guard against fixing this by matching too broadly."""
    from datetime import datetime

    fired = [f"{datetime(2026, 9, d):%a}" for d in range(6, 13)
             if plugin._cron_matches("30 2 * * 7", datetime(2026, 9, d, 2, 30))]
    assert len(fired) == 1
