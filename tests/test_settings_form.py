"""The Dispatcharr 0.34 settings form built by settings_form.py and Plugin.get_fields.

0.33 and earlier read the flat fields in plugin.json and drop any field type
they do not know, so the flat form must keep only the old types. 0.34 calls
get_fields, which rebuilds the same settings into sections, one pick list and
one taller text box. A field missing from the 0.34 form is silent (the loader
drops nothing visibly), so these tests check every id by name.
"""

import io
import json
import logging
import os
import pathlib
import threading
import unicodedata

import pytest

from iptv_checker import settings_form as sf

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / "iptv_checker" / "plugin.json"
_DATA = json.loads(io.open(_MANIFEST, encoding="utf-8").read())
FLAT = _DATA["fields"]
FLAT_IDS = [f["id"] for f in FLAT]
OLD_TYPES = {"string", "number", "boolean", "select", "text", "info"}


def _form(settings=None):
    return sf.build_form(FLAT, settings)


# ---- plugin.json ---------------------------------------------------------

def test_manifest_declares_version_1_and_capabilities():
    assert _DATA["manifest_version"] == 1, "version 2 turns on the sandbox; that needs its own decision"
    assert set(_DATA["capabilities"]) == {"outbound_network", "subprocess", "filesystem_write"}


def test_flat_form_holds_only_types_that_033_understands():
    assert {f["type"] for f in FLAT} <= OLD_TYPES
    for f in FLAT:
        for attr in ("section", "collapsed", "rows", "columns", "accept", "max_size"):
            assert attr not in f, f"{f['id']} carries 0.34-only attribute {attr}"


# ---- the 0.34 form ---------------------------------------------------------

def test_every_flat_id_is_in_the_034_form_or_mapped_to_a_new_id():
    ids = {f["id"] for f in _form()}
    for fid in FLAT_IDS:
        assert fid in ids or sf.NEW_ID_BY_OLD_ID.get(fid) in ids, fid


def test_form_ids_are_unique():
    ids = [f["id"] for f in _form()]
    assert len(ids) == len(set(ids))


def test_every_section_header_becomes_a_section_with_its_text_kept():
    headers = [f for f in FLAT if sf.is_section_header(f)]
    assert len(headers) == 18
    form = {f["id"]: f for f in _form()}
    for h in headers:
        assert form[h["id"]]["type"] == "section"
        about = form[h["id"] + sf.ABOUT_SUFFIX]
        assert about["description"] == h["description"]
        assert about["section"] == h["id"]


def test_every_field_after_the_first_header_names_its_section():
    current = None
    by_id = {f["id"]: f for f in _form()}
    for f in FLAT:
        if sf.is_section_header(f):
            current = f["id"]
            continue
        fid = sf.NEW_ID_BY_OLD_ID.get(f["id"], f["id"])
        assert by_id[fid].get("section") == current, fid


def test_every_section_member_names_a_declared_section():
    form = _form()
    sections = {f["id"] for f in form if f["type"] == "section"}
    for f in form:
        if "section" in f:
            assert f["section"] in sections, f["id"]


def test_layout_tables_name_only_real_ids():
    header_ids = {f["id"] for f in FLAT if sf.is_section_header(f)}
    assert sf.COLLAPSED_SECTIONS <= header_ids
    assert set(sf.TEXT_ROWS) <= set(FLAT_IDS)
    assert set(sf.HELP_PARAGRAPHS) <= set(FLAT_IDS)
    assert set(sf.NEW_ID_BY_OLD_ID) <= set(FLAT_IDS)


def test_the_four_everyday_sections_start_open():
    form = {f["id"]: f for f in _form()}
    for sid in ("_section_scope", "_section_check_behavior", "_section_scheduling", "_section_auto_run"):
        assert form[sid]["collapsed"] is False
    assert form["_section_auto_delete"]["collapsed"] is True


def test_scheduled_times_becomes_a_taller_text_box_under_the_same_id():
    f = {x["id"]: x for x in _form()}["scheduled_times"]
    assert f["type"] == "text" and f["rows"] == 3


def test_channel_groups_stays_free_text_because_it_takes_wildcards():
    f = {x["id"]: x for x in _form()}["channel_groups"]
    assert f["type"] == "string"


def test_long_help_is_split_into_a_short_line_and_titled_paragraphs():
    form = _form()
    by_id = {f["id"]: f for f in form}
    flat_by_id = {f["id"]: f for f in FLAT}
    for fid, (short, paras) in sf.HELP_PARAGRAPHS.items():
        assert by_id[fid]["help_text"] == short
        assert len(short) < len(flat_by_id[fid]["help_text"])
        ids = [f["id"] for f in form]
        pos = ids.index(fid)
        for n, (title, text) in enumerate(paras, start=1):
            extra = form[pos + n]
            assert extra["id"] == f"{fid}{sf.HELP_SUFFIX}{n}"
            assert extra["label"] == title and extra["description"] == text
            assert extra.get("section") == by_id[fid].get("section")


def test_building_the_form_does_not_modify_the_flat_list():
    before = json.dumps(FLAT, sort_keys=True)
    _form({"video_format_suffixes": "HD"})
    assert json.dumps(FLAT, sort_keys=True) == before


# ---- the video format pick list --------------------------------------------

def _suffix_field(settings=None):
    return {f["id"]: f for f in _form(settings)}["video_format_suffix_list"]


def test_pick_list_default_comes_from_the_saved_text_value():
    f = _suffix_field({"video_format_suffixes": "hd, SD"})
    assert f["type"] == "multiselect"
    assert f["default"] == ["HD", "SD"]


def test_pick_list_default_without_a_saved_value_is_the_flat_default():
    assert _suffix_field()["default"] == ["UHD", "FHD", "HD", "SD", "Unknown"]


def test_every_saved_value_is_an_option_even_an_unknown_one():
    f = _suffix_field({"video_format_suffixes": "HD, 4K", "video_format_suffix_list": ["8K"]})
    values = [o["value"] for o in f["options"]]
    assert {"HD", "4K", "8K"} <= set(values)
    assert set(f["default"]) <= set(values)
    assert values[:5] == list(sf.VIDEO_FORMATS)


def test_write_back_round_trip():
    out = sf.legacy_settings({"video_format_suffix_list": ["UHD", " HD "], "x": 1})
    assert out["video_format_suffixes"] == "UHD, HD"
    assert out["x"] == 1
    assert sf.legacy_settings({"video_format_suffix_list": []})["video_format_suffixes"] == ""


def test_write_back_ignores_a_value_that_is_not_a_list():
    out = sf.legacy_settings({"video_format_suffix_list": "HD", "video_format_suffixes": "SD"})
    assert out["video_format_suffixes"] == "SD"


def test_write_back_does_not_modify_its_input():
    s = {"video_format_suffix_list": ["HD"]}
    sf.legacy_settings(s)
    assert "video_format_suffixes" not in s


# ---- Plugin.get_fields and the write-back gate --------------------------------

def test_get_fields_returns_the_034_form(plugin):
    fields = plugin.get_fields({"settings": {}})
    assert any(f["type"] == "section" for f in fields)
    assert "video_format_suffix_list" in {f["id"] for f in fields}


def test_get_fields_falls_back_to_the_flat_form_on_an_error(plugin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")
    monkeypatch.setattr(sf, "build_form", boom)
    assert [f["id"] for f in plugin.get_fields({"settings": {}})] == FLAT_IDS


def test_write_back_applies_only_on_034(plugin, pmod, monkeypatch):
    s = {"video_format_suffix_list": ["HD"], "video_format_suffixes": "SD"}
    monkeypatch.setattr(pmod, "_settings_form_034", False)
    assert plugin._legacy_settings(s)["video_format_suffixes"] == "SD"
    monkeypatch.setattr(pmod, "_settings_form_034", True)
    assert plugin._legacy_settings(s)["video_format_suffixes"] == "HD"


def test_run_hands_the_written_back_value_to_the_action(plugin, pmod, monkeypatch):
    monkeypatch.setattr(pmod, "_settings_form_034", True)
    seen = {}

    def action(settings, logger):
        seen.update(settings)
        return {"status": "ok", "message": "done"}

    plugin.add_video_format_suffix_action = action
    plugin._report_usage = lambda *a, **k: None
    plugin.run("add_video_format_suffix", {}, {
        "settings": {"video_format_suffix_list": ["FHD"], "video_format_suffixes": "SD"},
        "logger": logging.getLogger("test")})
    assert seen["video_format_suffixes"] == "FHD"


def test_fresh_settings_writes_back_for_the_scheduled_path(plugin, pmod, monkeypatch):
    import sys
    import types
    monkeypatch.setattr(pmod, "_settings_form_034", True)
    row = types.SimpleNamespace(settings={"video_format_suffix_list": ["UHD"], "video_format_suffixes": "SD"})
    mgr = types.SimpleNamespace(filter=lambda **k: types.SimpleNamespace(first=lambda: row))
    models = types.ModuleType("apps.plugins.models")
    models.PluginConfig = types.SimpleNamespace(objects=mgr)
    monkeypatch.setitem(sys.modules, "apps.plugins.models", models)
    assert plugin._fresh_settings({})["video_format_suffixes"] == "UHD"


# ---- scheduler thread hand-over across a plugin reload -------------------------

def _named_thread(pmod, with_event):
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, args=(5,), name=pmod.SCHEDULER_THREAD_NAME, daemon=True)
    if with_event:
        setattr(t, pmod.SCHEDULER_STOP_ATTR, ev)
    t.start()
    return t, ev


def test_a_scheduler_thread_from_a_previous_load_is_asked_to_exit(plugin, pmod):
    t, ev = _named_thread(pmod, with_event=True)
    try:
        plugin._stop_superseded_scheduler_threads()
        t.join(2)
        assert ev.is_set() and not t.is_alive()
    finally:
        ev.set()


def test_a_thread_from_an_older_version_is_reported_and_left_alone(plugin, pmod, caplog):
    t, ev = _named_thread(pmod, with_event=False)
    try:
        with caplog.at_level(logging.WARNING):
            plugin._stop_superseded_scheduler_threads()
        assert not ev.is_set() and t.is_alive()
        assert "older plugin version" in caplog.text
    finally:
        ev.set()


def test_superseded_list_skips_the_current_thread_and_other_names(pmod):
    t, ev = _named_thread(pmod, with_event=True)
    other = threading.Thread(target=ev.wait, args=(5,), name="something-else", daemon=True)
    other.start()
    try:
        found = pmod.Plugin._superseded_scheduler_threads([t, other], t)
        assert found == []
        found = pmod.Plugin._superseded_scheduler_threads([t, other], threading.current_thread())
        assert found == [(t, ev)]
    finally:
        ev.set()


def test_started_scheduler_thread_carries_its_stop_event(plugin, pmod, monkeypatch):
    if not pmod.PYTZ_AVAILABLE:
        pytest.skip("pytz not installed")
    monkeypatch.setattr(plugin, "_scheduler_lock_taken_over", lambda: False)
    monkeypatch.setattr(plugin, "_dispatcharr_timezone", lambda: "UTC")
    plugin.check_progress = {"status": "idle"}
    plugin._start_background_scheduler({"scheduled_times": "0 4 * * *"})
    t = pmod._bg_scheduler_thread
    ev = getattr(t, pmod.SCHEDULER_STOP_ATTR, None) or threading.Event()
    try:
        assert hasattr(t, pmod.SCHEDULER_STOP_ATTR)
        assert t.name == pmod.SCHEDULER_THREAD_NAME
        plugin._stop_background_scheduler()
        assert ev.is_set() and not t.is_alive()
    finally:
        ev.set()


# ---- partial result ----------------------------------------------------------

def test_clear_csv_reports_partial_when_a_file_cannot_be_deleted(plugin, pmod, monkeypatch, tmp_path):
    monkeypatch.setattr(pmod.PluginConfig, "EXPORTS_DIR", str(tmp_path))
    for name in ("iptv_checker_results_a.csv", "iptv_checker_results_b.csv"):
        (tmp_path / name).write_text("x")
    real_remove = os.remove

    def remove(path):
        if path.endswith("_b.csv"):
            raise PermissionError("locked")
        real_remove(path)
    monkeypatch.setattr(pmod.os, "remove", remove)
    result = plugin.clear_csv_exports_action({}, logging.getLogger("test"))
    assert result["status"] == "partial"
    assert "message" in result and "error" not in result


def test_run_toast_marks_partial_with_a_warning_sign(plugin, pmod, monkeypatch):
    sent = []
    monkeypatch.setattr(pmod, "send_websocket_update", lambda *a, **k: sent.append(a[2]["message"]))
    plugin.clear_csv_exports_action = lambda s, lg: {"status": "partial", "message": "half"}
    plugin._report_usage = lambda *a, **k: None
    plugin.run("clear_csv_exports", {}, {"settings": {}, "logger": logging.getLogger("test")})
    assert sent and sent[-1].startswith(chr(0x26A0))


# ---- copy rules on the new strings -------------------------------------------

def _new_texts():
    out = []
    for new_id, meta in sf.NEW_FIELDS.items():
        out += [(f"{new_id}.label", meta["label"]), (f"{new_id}.help_text", meta["help_text"])]
    for fid, (short, paras) in sf.HELP_PARAGRAPHS.items():
        out.append((f"{fid}.short", short))
        for n, (title, text) in enumerate(paras, start=1):
            out += [(f"{fid}.{n}.title", title), (f"{fid}.{n}.text", text)]
    return out


@pytest.mark.parametrize("name,text", _new_texts(), ids=[n for n, _ in _new_texts()])
def test_new_text_follows_the_interface_copy_rules(name, text):
    assert chr(0x2014) not in text and chr(0x2013) not in text, "em or en dash"
    assert "--" not in text, "double hyphen"
    assert "\n" not in text, "help text is one paragraph"
    for ch in text:
        if ord(ch) >= 128:
            cat = unicodedata.category(ch)
            assert not (cat.startswith("P") or cat == "Zs" or cat == "Cf"), f"non-ASCII punctuation {hex(ord(ch))}"
