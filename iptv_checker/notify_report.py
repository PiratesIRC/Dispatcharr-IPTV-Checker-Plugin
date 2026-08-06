"""Send the report through Newsflasharr, and refuse loudly when it would not
actually arrive by email.

`notify_fn` IS INJECTED into every function here. This module never imports
notify_client, which keeps it unit-testable with no vendored dependency and no
Dispatcharr import. The caller passes `notify_client.notify`.

THE CLOSED KEYWORD SET. notify() accepts exactly: source, title, event, body,
severity, kind, dedup_key, url, attachment, base_dir. THERE IS NO FIELD FOR
ARBITRARY DATA. Nothing structured can travel this path, so do not plan to send
values through it and do not parse prose back out of a body: that is not a
contract worth depending on. Adding a field is a six-project change.

"QUEUED FOR DELIVERY", NEVER "SENT". notify() returning True means the event
was durably SPOOLED. Newsflasharr delivers it later on its own retry ladder, and
even an SMTP 250 is acceptance for relay rather than proof of delivery. Wording
that claims more than that is a lie the operator cannot check.

WHY THE PREFLIGHT EXISTS. Where an event lands is decided by Newsflasharr's
routing rules, not by this plugin. Without a rule matching this source, the
event still spools SUCCESSFULLY and is delivered somewhere else, most likely as
a push notification. That outcome is indistinguishable from working: notify()
returns True either way. So the routing is simulated before claiming the report
will arrive by email.
"""

import json

SOURCE = "iptv_checker"
EVENT_REPORT = "usage_report"

# Setting ids read from Newsflasharr's own configuration. Only the NAMES of
# missing keys are ever reported, never their values: one of these is a
# password.
#
# THESE NAMES ARE TAKEN FROM newsflasharr/channels.py, NOT GUESSED. An earlier
# version checked `smtp_host` and `smtp_port`, neither of which exists there,
# so the preflight could never pass no matter how the operator configured
# SMTP. A gate that cannot succeed is worse than no gate: it refuses a working
# setup and gives a reason that cannot be acted on.
#
# Newsflasharr treats SMTP as configured when smtp_server parses to a host, the
# recipient list is non-empty, and a From address is available. The port is part
# of smtp_server rather than a separate field.
SMTP_REQUIRED_KEYS = ("smtp_server", "smtp_to")
# The From address comes from smtp_from, falling back to smtp_username, so
# EITHER satisfies it. A password is NOT required: an unauthenticated relay is
# supported.
SMTP_FROM_KEYS = ("smtp_from", "smtp_username")


def parse_routing_rules(raw):
    """Newsflasharr stores routing_rules as a JSON STRING, not a list.

    Treating the string as a list iterates its CHARACTERS, which silently
    produces a rule set of single-character entries that match nothing. Returns
    a list, always, and never raises.
    """
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [r for r in parsed if isinstance(r, dict)]


def _rule_matches(event, match):
    """Mirror of newsflasharr/routing.py `_rule_matches`.

    NOTE THE EMPTY-MATCH CASE: `all([])` is True, so a rule with no `match` key
    matches EVERY event. Newsflasharr's own linter calls that out because a
    one-letter typo turns a targeted rule into a match-everything rule. This
    simulation must reproduce it, or the preflight would report a clean routing
    that the real evaluator does not agree with.
    """
    if not isinstance(match, dict):
        return False
    return all(event.get(key) == value for key, value in match.items())


def route(event, rules, default_channels):
    """Mirror of newsflasharr/routing.py `route`, so the preflight predicts the
    REAL destination rather than merely confirming a rule exists.

    THIS IS A DUPLICATED ALGORITHM AND THAT IS A KNOWN COST. A plugin cannot
    import another plugin, so the alternative is no preflight at all. If
    Newsflasharr's routing changes, this drifts silently, and the symptom is a
    preflight that passes while mail does not arrive. Re-read
    newsflasharr/routing.py when touching this.
    """
    rules = rules or []
    default_channels = list(default_channels or [])
    matched = [r for r in rules if _rule_matches(event, r.get("match", {}))]
    if not matched:
        return list(dict.fromkeys(default_channels))
    exclusive = any(r.get("exclusive") for r in matched)
    channels = [] if exclusive else list(default_channels)
    for rule in matched:
        extra = rule.get("channels")
        if isinstance(extra, list):
            channels.extend(extra)
    return list(dict.fromkeys(channels))


def preflight(newsflasharr_settings, event=EVENT_REPORT, source=SOURCE, want="smtp"):
    """Would a report from this plugin actually reach `want`?

    Returns (ok, problems). `problems` is a list of plain sentences naming what
    to fix. It never contains a setting VALUE, because one of the keys checked
    is a password.

    A False here is the difference between "the event was accepted" and "the
    operator will receive it", which notify() alone cannot tell them.
    """
    problems = []
    settings = newsflasharr_settings if isinstance(newsflasharr_settings, dict) else {}
    if not settings:
        return False, ["Newsflasharr settings could not be read, so where this "
                       "report would be delivered is unknown."]

    rules = parse_routing_rules(settings.get("routing_rules", "[]"))
    defaults = settings.get("default_channels") or []
    if isinstance(defaults, str):
        defaults = [c.strip() for c in defaults.split(",") if c.strip()]

    destinations = route({"source": source, "event": event}, rules, defaults)
    if want not in destinations:
        problems.append(
            "No Newsflasharr routing rule sends source %r event %r to %s. It would "
            "go to %s instead. Add a rule matching that source and event, placed "
            "before any catch-all rule that claims it first."
            % (source, event, want, ", ".join(destinations) if destinations else "nowhere"))

    if want == "smtp":
        missing = [k for k in SMTP_REQUIRED_KEYS if not settings.get(k)]
        if not any(settings.get(k) for k in SMTP_FROM_KEYS):
            missing.append(" or ".join(SMTP_FROM_KEYS))
        if missing:
            # Names only. Never a value: one of these settings is a password.
            problems.append("Newsflasharr SMTP settings are incomplete. Missing: "
                            + ", ".join(missing) + ".")

    return (not problems), problems


def build_body(model):
    """The email body. Plain lines a human reads, never a data channel.

    Total over its input: a junk model produces a short honest body rather than
    an exception, because this runs on the same path as the report write.
    """
    model = model if isinstance(model, dict) else {}
    totals = model.get("totals") if isinstance(model.get("totals"), dict) else {}
    health = model.get("run_health") if isinstance(model.get("run_health"), dict) else {}

    lines = [
        "%s channels checked, %s playing."
        % (totals.get("channels", 0), totals.get("channels_working", 0)),
    ]

    for section in model.get("sections") or []:
        if not isinstance(section, dict):
            continue
        count = len(section.get("rows") or [])
        if count:
            lines.append("%s: %d" % (section.get("title") or "group", count))

    if totals.get("channels_no_issues") is not None:
        lines.append("%s channels had no issues and are not listed."
                     % totals.get("channels_no_issues", 0))

    if not health.get("trustworthy", True):
        lines.append("WARNING: the provider rate limited %s stream request(s) during "
                     "this run, so some results may be wrong. Re-run before acting."
                     % health.get("rate_limited_streams", 0))

    detectors = health.get("detectors") if isinstance(health.get("detectors"), dict) else {}
    off = sorted(k for k, v in detectors.items() if not v)
    if off:
        lines.append("Not measured, so a zero for these means nobody looked: "
                     + ", ".join(off) + ".")

    return "\n".join(lines)


def emit_report(notify_fn, model, attachment_path=None):
    """Queue the report for delivery. Returns (ok, reason_or_None).

    The reason matters because notify() NEVER RAISES: it returns False when the
    spool refuses the event. Without a reason the operator is told it was not
    queued, with no cause, which closes only half of the silence.

    A failure reason carries an exception's TYPE NAME only, never str(exc).
    Provider credentials live inside stream URLs in this deployment and this
    string is rendered to the operator.
    """
    try:
        kwargs = {
            "source": SOURCE,
            "event": EVENT_REPORT,
            "severity": "info",
            "kind": "event",
            "title": "IPTV Checker report",
            "body": build_body(model),
        }
        if attachment_path:
            kwargs["attachment"] = attachment_path
        if notify_fn(**kwargs):
            return True, None
        return False, ("Newsflasharr declined the event. The spool may be full, or "
                       "it could not be written.")
    except Exception as exc:
        return False, "could not queue the report: %s" % type(exc).__name__
