"""Schema guard for plugin.json after the black->blank rename + reorganization.

Protects two invariants the reorg/rename must never break:
  (a) the exact set of field ids and action ids is frozen (reorder must not
      drop / rename / duplicate any id, since the DB keys settings by id);
  (b) no user-facing "Black Screen"/"Black-Screen" vocabulary leaks back into a
      field label/description or an action label/button/confirm message.

help_text is intentionally EXCLUDED from rule (b): one help_text legitimately
documents the literal `error_type 'Black Screen'` value that plugin.py still sets.
"""
import io
import json
from pathlib import Path

PLUGIN_JSON = Path(__file__).resolve().parents[1] / "iptv_checker" / "plugin.json"

FIELD_IDS = {
    '_section_scope', 'channel_groups', 'channel_groups_mode', 'check_alternative_streams',
    'only_visible_channels', '_section_check_behavior', 'timeout', 'probe_timeout',
    'dead_connection_retries', 'enable_parallel_checking', 'parallel_workers', 'stream_check_delay',
    '_section_black_screen', 'black_screen_detection', 'black_screen_sample_seconds',
    'black_screen_min_black_seconds', 'black_screen_ffmpeg_timeout',
    '_section_placeholder_file', 'placeholder_file_detection',
    '_section_frozen_video', 'frozen_video_detection', 'frozen_video_min_seconds',
    '_section_silent_audio', 'silent_audio_detection', 'silent_audio_max_db',
    '_section_post_check',
    '_section_dead', 'dead_rename_format', 'move_to_group_name', '_section_black',
    'black_screen_rename_format', 'move_black_screen_group', '_section_low_fps',
    'low_framerate_rename_format', 'move_low_framerate_group', '_section_format',
    'video_format_suffixes', '_section_restore',
    '_section_scheduling', 'scheduled_times', 'schedule_window_enabled', 'schedule_end_mode',
    'schedule_duration_hours', 'schedule_end_time', '_section_auto_run', 'scheduler_export_csv',
    'csv_retention_days',
    'scheduler_email_report',
    'scheduler_restore_channels', 'scheduler_rename_dead_channels',
    'scheduler_rename_black_screen_channels', 'scheduler_rename_low_framerate_channels',
    'scheduler_add_video_format_suffix', 'scheduler_move_dead_channels',
    'scheduler_move_black_screen_channels', 'scheduler_move_low_framerate_channels',
    'scheduler_delete_dead_channels', 'auto_delete_confirmation',
    '_section_advanced', 'ffprobe_flags', 'ffprobe_analysis_duration', 'streamlink_hosts',
    'ffprobe_path', 'ffmpeg_path',
}
ACTION_IDS = {
    'validate_settings', 'update_schedule', 'reset_progress', 'check_scheduler_status',
    'load_groups', 'check_streams', 'view_progress', 'cancel_check', 'view_results',
    'rename_channels', 'move_dead_channels', 'rename_black_screen_channels',
    'move_black_screen_channels', 'restore_channels', 'rename_low_framerate_channels',
    'move_low_framerate_channels', 'add_video_format_suffix', 'view_table', 'export_results',
    'cleanup_orphaned_tasks', 'clear_csv_exports', 'delete_dead_channels',
    'email_report',
}

_DATA = json.load(io.open(PLUGIN_JSON, encoding="utf-8"))


def test_field_ids_frozen():
    assert {f["id"] for f in _DATA["fields"]} == FIELD_IDS


def test_action_ids_frozen():
    assert {a["id"] for a in _DATA["actions"]} == ACTION_IDS


def test_no_field_id_duplicates():
    ids = [f["id"] for f in _DATA["fields"]]
    assert len(ids) == len(set(ids))


def test_no_black_screen_vocab_in_user_labels():
    bad = []
    for f in _DATA["fields"]:
        for key in ("label", "description"):
            val = f.get(key, "")
            if "Black Screen" in val or "Black-Screen" in val:
                bad.append((f["id"], key))
    for a in _DATA["actions"]:
        for key in ("label", "description", "button_label"):
            val = a.get(key, "")
            if "Black Screen" in val or "Black-Screen" in val:
                bad.append((a["id"], key))
        msg = a.get("confirm", {}).get("message", "")
        if "Black Screen" in msg or "Black-Screen" in msg:
            bad.append((a["id"], "confirm.message"))
    assert bad == [], f"Black-Screen vocabulary leaked into user-facing text: {bad}"


def test_blank_rename_actually_applied():
    # positive check: the detection toggle now says "Blank-Screen"
    det = next(f for f in _DATA["fields"] if f["id"] == "black_screen_detection")
    assert "Blank-Screen" in det["label"]


def test_keep_stable_group_default():
    # the destination group default must NOT drift (avoids orphaning) and must
    # match the plugin.py .get(...) fallbacks.
    mg = next(f for f in _DATA["fields"] if f["id"] == "move_black_screen_group")
    assert mg["default"] == "Black Screens"


def test_every_settable_field_has_label_and_type():
    for f in _DATA["fields"]:
        assert f.get("label"), f["id"]
        assert f.get("type"), f["id"]


def test_low_framerate_label_matches_the_actual_threshold():
    """The label said "Less than 30fps" long after the threshold moved to 24,
    so the plugin's own UI told users the wrong number. Bind the text to the
    constant that decides it."""
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("ic_cfg_probe", root / "iptv_checker" / "reports.py")
    reports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reports)
    threshold = reports.LOW_FRAMERATE_THRESHOLD

    field = next(f for f in _DATA["fields"] if f["id"] == "low_framerate_rename_format")
    text = (field.get("label", "") + " " + field.get("help_text", ""))
    assert "30fps" not in text and "30 fps" not in text, \
        "the low-framerate label still names 30fps"
    assert str(threshold) in text, \
        "the label should name the real threshold, %s" % threshold
