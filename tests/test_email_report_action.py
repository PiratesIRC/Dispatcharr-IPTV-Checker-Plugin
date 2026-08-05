"""The Email Report button, the scheduled equivalent, and their placement.

The failure this file is built around: a routing rule lives in ANOTHER plugin's
settings, so without it the report still spools successfully and is delivered
somewhere else. Every signal available here reads healthy. The action therefore
refuses loudly rather than reporting a success the operator cannot verify.
"""
import io
import json
import pathlib

PLUGIN_JSON = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "plugin.json"
PLUGIN_PY = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "plugin.py"
_DATA = json.load(io.open(str(PLUGIN_JSON), encoding="utf-8"))


# ---- placement, which is what was asked for -----------------------------

def test_the_button_sits_directly_after_export_to_csv():
    ids = [a["id"] for a in _DATA["actions"]]
    assert "email_report" in ids
    assert ids[ids.index("export_results") + 1] == "email_report"


def test_the_scheduled_setting_sits_directly_after_the_csv_setting():
    ids = [f["id"] for f in _DATA["fields"]]
    assert "scheduler_email_report" in ids
    assert ids[ids.index("scheduler_export_csv") + 1] == "scheduler_email_report"


def test_the_scheduled_setting_defaults_to_off():
    field = next(f for f in _DATA["fields"] if f["id"] == "scheduler_email_report")
    assert field["default"] is False
    assert field["type"] == "boolean"


# ---- declared is not routed ---------------------------------------------

def test_every_declared_action_is_actually_routed():
    """An action declared in plugin.json with no branch in the action_map
    renders on the card, passes an exact-set schema test, and returns the
    generic unknown-action error when pressed. Checking the declaration alone
    is a false pass."""
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    start = source.index("action_map = {")
    end = source.index("}", start)
    routed = set()
    for line in source[start:end].split("\n"):
        line = line.strip()
        if line.startswith('"') and '":' in line:
            routed.add(line.split('"')[1])
    declared = {a["id"] for a in _DATA["actions"]}
    missing = sorted(declared - routed)
    assert not missing, "declared but NOT routed, so pressing them fails: %s" % missing


def test_the_email_report_action_is_routed():
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    assert '"email_report": self.email_report_action,' in source


# ---- the action's behaviour ---------------------------------------------

class _Written(dict):
    pass


def _wire(plugin, monkeypatch, tmp_path, results, nf_settings, queued=True,
          queue_reason=None):
    """Drive the real action with the report modules live and only the two
    Dispatcharr-facing seams replaced."""
    monkeypatch.setattr(plugin, "_load_json_file", lambda path: results)
    monkeypatch.setattr(plugin, "_newsflasharr_settings", lambda logger: nf_settings)
    import iptv_checker.plugin as pm
    monkeypatch.setattr(pm.PluginConfig, "REPORT_DIR", str(tmp_path))

    import iptv_checker.notify_report as nr
    monkeypatch.setattr(nr, "emit_report",
                        lambda fn, model, attachment_path=None: (queued, queue_reason))
    return plugin


_RESULTS = [
    {"channel_id": 1, "channel_name": "Dead", "stream_id": 10,
     "status": "Dead", "error_type": "Timeout", "framerate_num": 0},
    {"channel_id": 2, "channel_name": "Fine", "stream_id": 20,
     "status": "Alive", "error_type": "N/A", "framerate_num": 50},
]

_GOOD_NF = {
    "routing_rules": json.dumps([{"match": {"source": "iptv_checker",
                                            "event": "usage_report"},
                                  "channels": ["smtp"], "exclusive": True}]),
    "default_channels": "ticker",
    "smtp_host": "mail.example", "smtp_port": "587",
    "smtp_from": "a@example.com", "smtp_to": "b@example.com",
    "smtp_password": "set",
}

_NO_RULE_NF = dict(_GOOD_NF, routing_rules="[]")


def test_writes_the_report_and_queues_it(plugin, monkeypatch, tmp_path, quiet_logger):
    _wire(plugin, monkeypatch, tmp_path, _RESULTS, _GOOD_NF)
    res = plugin.email_report_action({}, quiet_logger)
    assert res["status"] == "ok"
    assert "file" in res and pathlib.Path(res["file"]).exists()
    assert "queued for delivery" in res["message"].lower()


def test_the_result_never_claims_the_email_was_sent(plugin, monkeypatch, tmp_path, quiet_logger):
    """notify() returning True means durably SPOOLED. Newsflasharr delivers
    later on its own retry ladder."""
    _wire(plugin, monkeypatch, tmp_path, _RESULTS, _GOOD_NF)
    res = plugin.email_report_action({}, quiet_logger)
    body = (res.get("message") or "").lower()
    for claim in ("was sent", "has been sent", "email sent", "successfully sent"):
        assert claim not in body


def test_a_missing_routing_rule_refuses_loudly_and_still_gives_the_path(
        plugin, monkeypatch, tmp_path, quiet_logger):
    """The whole point. The event would spool successfully and be delivered
    somewhere else, which is indistinguishable from working."""
    _wire(plugin, monkeypatch, tmp_path, _RESULTS, _NO_RULE_NF)
    res = plugin.email_report_action({}, quiet_logger)
    assert res["status"] == "error"
    assert "error" in res and "message" not in res
    assert "NOT arrive by email" in res["error"]
    assert pathlib.Path(res["file"]).exists(), \
        "the report must still be written and its path returned"


def test_unreadable_newsflasharr_settings_refuse_rather_than_assume(
        plugin, monkeypatch, tmp_path, quiet_logger):
    _wire(plugin, monkeypatch, tmp_path, _RESULTS, None)
    res = plugin.email_report_action({}, quiet_logger)
    assert res["status"] == "error"
    assert pathlib.Path(res["file"]).exists()


def test_a_declined_queue_reports_the_reason_beside_the_path(
        plugin, monkeypatch, tmp_path, quiet_logger):
    _wire(plugin, monkeypatch, tmp_path, _RESULTS, _GOOD_NF,
          queued=False, queue_reason="spool full")
    res = plugin.email_report_action({}, quiet_logger)
    assert res["status"] == "error"
    assert "spool full" in res["error"]
    assert pathlib.Path(res["file"]).exists()


def test_no_results_is_an_error_not_an_empty_report(plugin, monkeypatch, tmp_path, quiet_logger):
    _wire(plugin, monkeypatch, tmp_path, [], _GOOD_NF)
    res = plugin.email_report_action({}, quiet_logger)
    assert res["status"] == "error"
    assert "check streams" in res["error"].lower()


def test_exactly_one_of_message_or_error_is_set(plugin, monkeypatch, tmp_path, quiet_logger):
    """A failure that sets neither renders identically to success on the card."""
    for nf, queued in ((_GOOD_NF, True), (_NO_RULE_NF, True), (_GOOD_NF, False)):
        _wire(plugin, monkeypatch, tmp_path, _RESULTS, nf, queued=queued)
        res = plugin.email_report_action({}, quiet_logger)
        assert ("message" in res) != ("error" in res), sorted(res)


def test_the_result_fits_the_toast(plugin, monkeypatch, tmp_path, quiet_logger):
    import iptv_checker.plugin as pm
    for nf in (_GOOD_NF, _NO_RULE_NF):
        _wire(plugin, monkeypatch, tmp_path, _RESULTS, nf)
        res = plugin.email_report_action({}, quiet_logger)
        body = res.get("message") or res.get("error")
        assert len(body) <= pm.PluginConfig.TOAST_BUDGET, len(body)


def test_the_report_goes_to_config_not_to_the_unauthenticated_logo_directory():
    """Dispatcharr's nginx serves /data/logos/ to the whole LAN with NO
    authentication, and /data/<plugin>/ is a named volume with no host path."""
    import iptv_checker.plugin as pm
    assert pm.PluginConfig.REPORT_DIR.startswith("/config/")
    # Strip whole comment LINES, not the "# " prefix: the comment explaining
    # why that directory is avoided legitimately names it, and a naive strip
    # left the text behind and failed on the explanation itself.
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    code_lines = [ln for ln in source.splitlines()
                  if not ln.lstrip().startswith("#")]
    code = chr(10).join(code_lines)
    assert "/data/logos" not in code, "code writes to the unauthenticated logo directory"


# ---- the scheduled path uses the same code ------------------------------

def test_the_scheduled_step_reads_exactly_its_own_setting():
    """A substring check is NOT enough. Renaming the key to
    scheduler_email_report_DISABLED still CONTAINS the original name, so a
    mutation that stopped the scheduled step ever running passed unnoticed.
    Assert the exact read instead."""
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    assert "settings.get('scheduler_email_report', False)" in source, \
        "the scheduled step does not read its own setting"


def test_the_scheduled_step_calls_the_shared_builder():
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    start = source.index("settings.get('scheduler_email_report', False)")
    window = source[start:start + 900]
    assert "_build_and_deliver_report" in window, \
        "the scheduled step must use the same builder as the button"


def test_the_scheduled_step_runs_before_the_mid_list_gate():
    """A window that closes part way through the channel list must still leave
    a report, on the same terms as the CSV export beside it."""
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    email_at = source.index("scheduler_email_report")
    gate_at = source.index("post-actions deferred to next window")
    assert email_at < gate_at


def test_a_delivery_failure_cannot_abort_the_scheduled_post_actions():
    source = io.open(str(PLUGIN_PY), encoding="utf-8").read()
    start = source.index("SCHEDULED: Building and emailing the report")
    window = source[start:start + 1200]
    assert "try:" in window and "except Exception" in window


# ---- the button text ----------------------------------------------------

def test_the_button_is_not_labelled_Run():
    """Without button_label, Dispatcharr renders the default text "Run", which
    says nothing about what the button does."""
    act = next(a for a in _DATA["actions"] if a["id"] == "email_report")
    label = act.get("button_label", "")
    assert label, "button_label is missing, so the button renders as Run"
    assert label.strip().lower() != "run"
    assert "Email Report" in label


def test_the_button_carries_a_colour_and_a_variant():
    """House convention: cyan for an action that leaves the box, filled because
    it acts rather than only reading. Both are plain strings to Dispatcharr's
    serializer, so the constraint is the frontend's palette, not validation."""
    act = next(a for a in _DATA["actions"] if a["id"] == "email_report")
    assert act.get("button_color") == "cyan"
    assert act.get("button_variant") == "filled"


def test_the_button_values_are_ones_the_frontend_understands():
    """A colour outside the frontend palette is the real risk here. These are
    the names already proven to render by sibling plugins on this Dispatcharr
    version."""
    known_colors = {"dark", "gray", "red", "pink", "grape", "violet", "indigo",
                    "blue", "cyan", "teal", "green", "lime", "yellow", "orange"}
    known_variants = {"filled", "light", "outline", "subtle", "default", "white"}
    for act in _DATA["actions"]:
        if act.get("button_color"):
            assert act["button_color"] in known_colors, (act["id"], act["button_color"])
        if act.get("button_variant"):
            assert act["button_variant"] in known_variants, (act["id"], act["button_variant"])


def test_every_action_carries_a_label_colour_and_variant():
    """All 23 already did except email_report, which was added without one and
    so rendered as the default "Run". This keeps the set complete: a new action
    added without these renders as Run and nothing else would say so."""
    missing = [(a["id"], key)
               for a in _DATA["actions"]
               for key in ("button_label", "button_color", "button_variant")
               if not a.get(key)]
    assert missing == [], "actions missing button styling: %s" % missing
