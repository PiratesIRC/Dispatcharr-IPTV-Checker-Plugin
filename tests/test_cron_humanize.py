"""Plain-language descriptions of cron expressions, shown in the interface.

The description appears when the operator saves a schedule and when they check
scheduler status, so a wrong description is worse than none: it would tell
somebody their check runs at a time it does not. Every expression the converter
cannot describe with certainty is therefore returned unchanged.

This is deliberately not the cron_descriptor package, which is importable inside
the container as a dependency of django_celery_beat. It cannot produce this
wording, it is not installed where these tests run, this plugin has never
declared it, and it describes the unrunnable expression "0 25 * * *" as
"At 13:00 PM" rather than declining.
"""


def test_a_daily_time(plugin):
    assert plugin._humanize_cron("35 8 * * *") == "Daily at 8:35 AM"


def test_a_daily_time_in_the_evening(plugin):
    assert plugin._humanize_cron("0 23 * * *") == "Daily at 11:00 PM"


def test_midnight_and_noon_are_not_zero_oclock(plugin):
    assert plugin._humanize_cron("0 0 * * *") == "Daily at 12:00 AM"
    assert plugin._humanize_cron("0 12 * * *") == "Daily at 12:00 PM"


def test_a_list_of_hours(plugin):
    """The expression from GitHub issue 27, which the parser accepts."""
    assert plugin._humanize_cron("0 0,8,16 * * *") == (
        "Daily at 12:00 AM, 8:00 AM and 4:00 PM"
    )


def test_a_list_of_minutes(plugin):
    assert plugin._humanize_cron("0,30 8 * * *") == "Daily at 8:00 AM and 8:30 AM"


def test_two_times_are_joined_with_and_not_a_comma(plugin):
    assert plugin._humanize_cron("0 6,18 * * *") == "Daily at 6:00 AM and 6:00 PM"


def test_a_minute_step(plugin):
    assert plugin._humanize_cron("*/15 * * * *") == "Every 15 minutes"


def test_an_hour_step(plugin):
    assert plugin._humanize_cron("0 */6 * * *") == "Every 6 hours"


def test_a_step_of_one_is_singular(plugin):
    assert plugin._humanize_cron("*/1 * * * *") == "Every minute"


def test_a_single_weekday_is_pluralised(plugin):
    assert plugin._humanize_cron("30 2 * * 0") == "Sundays at 2:30 AM"


def test_sunday_may_be_written_as_seven(plugin):
    assert plugin._humanize_cron("30 2 * * 7") == "Sundays at 2:30 AM"


def test_a_weekday_range(plugin):
    assert plugin._humanize_cron("0 4 * * 1-5") == "Mon to Fri at 4:00 AM"


def test_a_weekday_list(plugin):
    assert plugin._humanize_cron("0 9 * * 1,3,5") == "Mon, Wed and Fri at 9:00 AM"


def test_a_day_of_the_month(plugin):
    assert plugin._humanize_cron("15 3 1 * *") == "Day 1 of each month at 3:15 AM"


def test_a_month_is_named_in_full(plugin):
    assert plugin._humanize_cron("5 4 * 6 *") == "Daily at 4:05 AM in June"


def test_a_step_may_carry_a_weekday_qualifier(plugin):
    assert plugin._humanize_cron("*/30 * * * 1-5") == "Every 30 minutes, Mon to Fri"


# --- anything it cannot describe with certainty is returned unchanged --------

def test_a_non_cron_string_is_returned_unchanged(plugin):
    assert plugin._humanize_cron("bogus") == "bogus"


def test_an_hour_that_can_never_match_is_returned_unchanged(plugin):
    """The plugin's own parser accepts this, and it can never fire.

    cron_descriptor calls it "At 13:00 PM", which invents a time the schedule
    will never run at. Returning the expression says nothing false.
    """
    assert plugin._humanize_cron("0 25 * * *") == "0 25 * * *"


def test_a_minute_that_can_never_match_is_returned_unchanged(plugin):
    assert plugin._humanize_cron("70 8 * * *") == "70 8 * * *"


def test_an_unparseable_field_is_returned_unchanged(plugin):
    assert plugin._humanize_cron("0 abc * * *") == "0 abc * * *"


def test_too_many_times_to_list_is_returned_unchanged(plugin):
    """A long list would be clipped from the middle of the interface message."""
    expr = "0,10,20,30,40,50 8,9 * * *"
    assert plugin._humanize_cron(expr) == expr


def test_the_description_is_plain_ascii(plugin):
    """Interface copy in this project carries no dashes or other non-ASCII."""
    for expr in ("35 8 * * *", "0 4 * * 1-5", "0 9 * * 1,3,5", "0 0,8,16 * * *",
                 "30 2 * * 0", "15 3 1 * *", "*/15 * * * *", "5 4 * 6 *"):
        described = plugin._humanize_cron(expr)
        assert described.isascii(), f"{expr} produced non-ASCII: {described!r}"
        assert "--" not in described


# --- the description must reach the operator, not just exist -----------------

def _stub_scheduler(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_owns_scheduler_lock", lambda: False)
    monkeypatch.setattr(plugin, "_request_scheduler_reload", lambda: None)
    monkeypatch.setattr(plugin, "_dispatcharr_timezone", lambda: "America/Chicago")


def test_update_schedule_shows_the_description(plugin, monkeypatch, quiet_logger):
    _stub_scheduler(plugin, monkeypatch)

    result = plugin.update_schedule_action(
        {"scheduled_times": "35 8 * * *"}, quiet_logger)

    assert result["status"] == "ok"
    assert "Daily at 8:35 AM" in result["message"]


def test_update_schedule_describes_every_expression(plugin, monkeypatch, quiet_logger):
    _stub_scheduler(plugin, monkeypatch)

    result = plugin.update_schedule_action(
        {"scheduled_times": "35 8 * * *;0 23 * * *"}, quiet_logger)

    assert "Daily at 8:35 AM" in result["message"]
    assert "Daily at 11:00 PM" in result["message"]


def test_update_schedule_does_not_repeat_an_undescribable_expression(
        plugin, monkeypatch, quiet_logger):
    """Showing "0 25 * * * (0 25 * * *)" would be noise."""
    _stub_scheduler(plugin, monkeypatch)

    result = plugin.update_schedule_action(
        {"scheduled_times": "0 25 * * *"}, quiet_logger)

    assert "0 25 * * * (0 25 * * *)" not in result["message"]


def test_update_schedule_message_fits_the_toast_budget(
        plugin, pmod, monkeypatch, quiet_logger):
    """Dispatcharr clips a toast from the middle, which would hide the finding."""
    _stub_scheduler(plugin, monkeypatch)

    # Enough schedules that the message would overflow if it were not trimmed,
    # so removing the trim is caught rather than passing on a short message.
    result = plugin.update_schedule_action(
        {"scheduled_times": "0 0,8,16 * * *;30 2 * * 0;0 4 * * 1-5;"
                            "15 3 1 * *;0 9 * * 1,3,5;0,30 8 * * *"},
        quiet_logger)

    budget = pmod.PluginConfig.TOAST_BUDGET
    assert budget == 270, "the budget moved; this test is asserting the wrong thing"
    assert len(result["message"]) <= budget, (
        f"message is {len(result['message'])} characters: {result['message']!r}")
    assert result["message"].endswith("..."), (
        "the message was not long enough to need trimming, so this test would "
        "pass even with the trim removed")
