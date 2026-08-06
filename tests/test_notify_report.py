"""Queueing the report through Newsflasharr, and the preflight that refuses
when it would not actually arrive by email.

The failure this file exists to prevent: notify() returns True as soon as the
event is SPOOLED, regardless of where Newsflasharr later routes it. Without a
routing rule the report is delivered somewhere else, most likely as a push
notification, and every signal available to this plugin says it worked.
"""
import importlib.util
import json
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "notify_report.py"
_spec = importlib.util.spec_from_file_location("ic_notify_report", _PATH)
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)


def _settings(rules, defaults="ticker", **extra):
    """A COMPLETE Newsflasharr settings dict, using its REAL setting ids.

    These names come from newsflasharr/channels.py. An earlier version of this
    fixture invented smtp_host and smtp_port, which do not exist there, and the
    code under test invented the same two, so the tests and the implementation
    agreed with each other and were wrong together. The preflight could never
    pass against a real install. A test that mirrors the implementation's
    assumption cannot detect that the assumption is false.
    """
    base = {
        "routing_rules": json.dumps(rules) if not isinstance(rules, str) else rules,
        "default_channels": defaults,
        "smtp_server": "mail.example:587",
        "smtp_from": "a@example.com",
        "smtp_username": "a@example.com",
        "smtp_to": "b@example.com",
        "smtp_password": "set",
    }
    base.update(extra)
    return base


_SMTP_RULE = {"match": {"source": "iptv_checker", "event": "usage_report"},
              "channels": ["smtp"], "exclusive": True}


# ---- routing_rules is a JSON STRING, not a list -------------------------

def test_rules_parse_from_a_json_string():
    rules = nr.parse_routing_rules(json.dumps([_SMTP_RULE]))
    assert rules == [_SMTP_RULE]


def test_a_string_is_never_treated_as_a_list_of_characters():
    """Iterating the raw string yields single characters, which silently
    produces a rule set that matches nothing."""
    rules = nr.parse_routing_rules('[{"match": {"source": "x"}, "channels": ["smtp"]}]')
    assert all(isinstance(r, dict) for r in rules)
    assert len(rules) == 1


def test_junk_rules_never_raise():
    for junk in (None, "", "not json", "{}", "[1, 2]", 5, {}, "[[]]"):
        assert isinstance(nr.parse_routing_rules(junk), list)


# ---- the routing simulation must match Newsflasharr's real algorithm ----

def test_no_matching_rule_falls_through_to_the_defaults():
    assert nr.route({"source": "iptv_checker", "event": "usage_report"},
                    [], ["ticker"]) == ["ticker"]


def test_an_exclusive_match_drops_the_defaults():
    assert nr.route({"source": "iptv_checker", "event": "usage_report"},
                    [_SMTP_RULE], ["ticker"]) == ["smtp"]


def test_a_non_exclusive_match_adds_to_the_defaults():
    rule = {"match": {"source": "iptv_checker"}, "channels": ["smtp"]}
    assert nr.route({"source": "iptv_checker", "event": "usage_report"},
                    [rule], ["ticker"]) == ["ticker", "smtp"]


def test_a_rule_with_no_match_key_matches_every_event():
    """all([]) is True, so a rule missing its match key is a match-everything
    rule. Newsflasharr's own linter warns about this because a one-letter typo
    causes it. The simulation must reproduce it, or the preflight would predict
    a destination the real evaluator disagrees with."""
    catch_all = {"channels": ["ticker"], "exclusive": True}
    assert nr.route({"source": "iptv_checker", "event": "usage_report"},
                    [catch_all, _SMTP_RULE], ["push"]) == ["ticker", "smtp"]


def test_an_earlier_exclusive_catch_all_still_lets_a_later_rule_contribute():
    """Exclusivity drops the DEFAULTS, it does not stop other matching rules.
    Both matched rules contribute, in rule order."""
    catch_all = {"match": {"source": "iptv_checker"}, "channels": ["ticker"],
                 "exclusive": True}
    out = nr.route({"source": "iptv_checker", "event": "usage_report"},
                   [catch_all, _SMTP_RULE], ["push"])
    assert out == ["ticker", "smtp"]
    assert "push" not in out


def test_channels_are_deduped_and_order_stable():
    a = {"match": {"source": "iptv_checker"}, "channels": ["smtp", "ticker"]}
    b = {"match": {"event": "usage_report"}, "channels": ["ticker", "smtp"]}
    assert nr.route({"source": "iptv_checker", "event": "usage_report"},
                    [a, b], []) == ["smtp", "ticker"]


# ---- the preflight -------------------------------------------------------

def test_preflight_passes_with_a_matching_smtp_rule():
    ok, problems = nr.preflight(_settings([_SMTP_RULE]))
    assert ok is True
    assert problems == []


def test_preflight_refuses_when_no_rule_routes_to_smtp():
    """The whole point: the event would spool successfully and be delivered
    somewhere else, which is indistinguishable from working."""
    ok, problems = nr.preflight(_settings([]))
    assert ok is False
    assert any("routing rule" in p for p in problems)
    assert any("ticker" in p for p in problems), "it should say where it WOULD go"


def test_preflight_refuses_when_a_rule_matches_a_different_source():
    other = {"match": {"source": "sentinelarr", "event": "usage_report"},
             "channels": ["smtp"], "exclusive": True}
    ok, problems = nr.preflight(_settings([other]))
    assert ok is False


def test_preflight_refuses_when_a_rule_matches_a_different_event():
    other = {"match": {"source": "iptv_checker", "event": "something_else"},
             "channels": ["smtp"], "exclusive": True}
    ok, problems = nr.preflight(_settings([other]))
    assert ok is False


def test_preflight_reports_missing_smtp_keys_by_NAME_never_by_value():
    settings = _settings([_SMTP_RULE])
    settings["smtp_server"] = ""
    ok, problems = nr.preflight(settings)
    assert ok is False
    assert "smtp_server" in " ".join(problems)


def test_a_password_is_not_required():
    """Newsflasharr supports an unauthenticated relay, so demanding a password
    would refuse a working setup."""
    settings = _settings([_SMTP_RULE])
    settings["smtp_password"] = ""
    ok, problems = nr.preflight(settings)
    assert ok is True, problems


def test_smtp_username_alone_satisfies_the_from_address():
    """channels.py falls back to smtp_username when smtp_from is unset."""
    settings = _settings([_SMTP_RULE])
    settings["smtp_from"] = ""
    ok, problems = nr.preflight(settings)
    assert ok is True, problems


def test_no_from_address_at_all_is_reported():
    settings = _settings([_SMTP_RULE])
    settings["smtp_from"] = ""
    settings["smtp_username"] = ""
    ok, problems = nr.preflight(settings)
    assert ok is False
    assert "smtp_from" in " ".join(problems)


def test_the_checked_key_names_exist_in_newsflasharr():
    """The bug this file failed to catch: names invented in both the code and
    the fixture. Read the real ids from the sibling plugin where present."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "notifier" / "newsflasharr" / "plugin.py"
    if not src.exists():
        pytest.skip("Newsflasharr is not present in this checkout")
    text = src.read_text(encoding="utf-8", errors="replace")
    for key in nr.SMTP_REQUIRED_KEYS + nr.SMTP_FROM_KEYS:
        assert ('"id": "%s"' % key) in text, (
            "%s is not a Newsflasharr setting id, so the preflight can never pass" % key)


def test_preflight_never_leaks_a_setting_value():
    """The missing-key branch MUST actually fire, or this proves nothing. An
    earlier version of this test left every SMTP key populated, so `missing`
    was empty, the reporting line never ran, and a mutation that dumped the
    whole settings dict passed unnoticed. One key is therefore blanked while
    the others hold values that must never appear."""
    settings = _settings([])
    settings["smtp_password"] = "hunter2-should-never-appear"
    settings["smtp_host"] = "secret.internal.example"
    settings["smtp_to"] = ""            # forces the missing-key branch to run
    ok, problems = nr.preflight(settings)
    joined = " ".join(problems)
    assert ok is False
    assert "smtp_to" in joined, "the branch under test did not run"
    assert "hunter2-should-never-appear" not in joined
    assert "secret.internal.example" not in joined
    assert "smtp_from" not in joined or "a@example.com" not in joined


def test_no_smtp_setting_VALUE_ever_appears_in_the_readout():
    """Scoped to the SMTP settings, which is what the rule covers. The routing
    message deliberately NAMES the channel the report would go to instead, so
    `default_channels` appearing is the message working rather than a leak: a
    channel name is not a secret and the operator needs it to fix the routing."""
    settings = _settings([])
    settings["smtp_server"] = ""
    ok, problems = nr.preflight(settings)
    joined = " ".join(problems)
    for key in nr.SMTP_REQUIRED_KEYS + nr.SMTP_FROM_KEYS:
        value = settings.get(key)
        if isinstance(value, str) and value:
            assert value not in joined, "value of %s leaked into the readout" % key
        assert key in joined or settings.get(key),             "a missing key must be named so the operator can fix it"


def test_preflight_refuses_when_settings_cannot_be_read():
    for junk in (None, {}, "not a dict", 5):
        ok, problems = nr.preflight(junk)
        assert ok is False
        assert problems


def test_preflight_accepts_comma_separated_default_channels():
    ok, _ = nr.preflight(_settings([], defaults="ticker, smtp"))
    assert ok is True, "smtp reached via the defaults is still smtp"


# ---- the body ------------------------------------------------------------

def _model():
    spec = importlib.util.spec_from_file_location(
        "ic_reports_nb",
        pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "reports.py")
    reports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reports)
    rows = [
        {"channel_id": 1, "channel_name": "Dead", "stream_id": 10,
         "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
        {"channel_id": 2, "channel_name": "Fine", "stream_id": 20,
         "status": "Alive", "error_type": "N/A", "framerate_num": 50},
        {"channel_id": 3, "channel_name": "Throttled", "stream_id": 30,
         "status": "Skipped", "error_type": "Rate Limited", "framerate_num": 0},
    ]
    return reports.build_model(rows, {}, now=0, version="9.9.9")


def test_body_states_the_counts():
    body = nr.build_body(_model())
    assert "3 channels checked" in body


def test_body_warns_when_the_run_was_rate_limited():
    body = nr.build_body(_model())
    assert "rate limited" in body.lower()
    assert "re-run" in body.lower()


def test_body_names_the_detectors_that_did_not_run():
    """A zero from a detector that was OFF means nobody looked."""
    body = nr.build_body(_model())
    assert "nobody looked" in body.lower()
    assert "black_screen" in body


def test_body_is_total_over_junk():
    for junk in (None, {}, {"totals": None}, {"sections": [None]}, 5):
        assert isinstance(nr.build_body(junk), str)


# ---- emitting ------------------------------------------------------------

def test_emit_uses_only_the_closed_keyword_set():
    """notify() has a fixed signature. An unexpected keyword would raise inside
    the spool, and there is no field for arbitrary data."""
    seen = {}

    def fake_notify(**kwargs):
        seen.update(kwargs)
        return True

    ok, reason = nr.emit_report(fake_notify, _model(), attachment_path="/tmp/r.html")
    assert ok is True and reason is None
    allowed = {"source", "title", "event", "body", "severity", "kind",
               "dedup_key", "url", "attachment", "base_dir"}
    assert set(seen) <= allowed, set(seen) - allowed
    assert seen["source"] == nr.SOURCE
    assert seen["event"] == nr.EVENT_REPORT
    assert seen["attachment"] == "/tmp/r.html"


def test_emit_omits_the_attachment_key_when_there_is_none():
    seen = {}

    def fake_notify(**kwargs):
        seen.update(kwargs)
        return True

    nr.emit_report(fake_notify, _model())
    assert "attachment" not in seen


def test_a_declined_event_reports_a_reason():
    """notify() NEVER RAISES: it returns False. Without a reason the operator
    is told it was not queued, with no cause."""
    ok, reason = nr.emit_report(lambda **kw: False, _model())
    assert ok is False
    assert reason and "declined" in reason.lower()


def test_an_exception_reason_carries_the_type_name_only():
    """Provider credentials live inside stream URLs here, and this string is
    rendered to the operator."""
    def boom(**kwargs):
        raise RuntimeError("http://edge.example/live/USER/PASS/1.ts")

    ok, reason = nr.emit_report(boom, _model())
    assert ok is False
    assert "RuntimeError" in reason
    assert "USER" not in reason and "PASS" not in reason
    assert "edge.example" not in reason


def test_emit_never_raises():
    for fn in (None, "not callable", lambda: None):
        ok, reason = nr.emit_report(fn, _model())
        assert ok is False
        assert reason


# ---- wording -------------------------------------------------------------

def test_nothing_in_this_module_claims_the_report_was_sent():
    """notify() returning True means durably SPOOLED. Newsflasharr delivers
    later on its own retry ladder, and an SMTP 250 is acceptance for relay, not
    proof of delivery."""
    import io
    text = io.open(str(_PATH), encoding="utf-8").read()
    lowered = text.lower()
    for claim in ("was sent", "has been sent", "successfully sent", "email sent"):
        assert claim not in lowered, claim
