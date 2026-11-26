"""The dual-path rollout: this release honours the old /data coordination files while old workers run.

A worker on the previous release still uses /data/iptv_checker_scheduler.pid, the reload flag, the
fire claims folder and the progress file, and Stream-Mapparr still reads the progress file. These
tests prove each pairing. The election tests need a live foreign process and os.kill(pid, 0),
which terminates a process on Windows, so they run on Linux only (see docs/superpowers/linux-pytest.sh).
"""
import datetime
import logging
import os
import subprocess
import json
import sys
import time
import types

import pytest

import iptv_checker.plugin as pm
import iptv_checker.storage as storage

PosixOnly = pytest.mark.skipif(sys.platform == "win32",
                               reason="os.kill(pid, 0) terminates a process on Windows")
MINUTE = datetime.datetime(2026, 11, 25, 7, 7)
LOG = logging.getLogger("iptv_checker.tests.rollout")


@pytest.fixture
def rollout(tmp_path, monkeypatch):
    """A non-legacy layout: the new coordination files beside the data dir, the old ones under tmp."""
    cfg = pm.PluginConfig
    plugins = tmp_path / "plugins"
    layout = storage.Layout(False, str(plugins / "_data" / "iptv_checker"), str(plugins), "iptv_checker", "test")
    old = tmp_path / "old"
    old.mkdir()
    monkeypatch.setattr(cfg, "STORAGE_LAYOUT", layout)
    monkeypatch.setattr(cfg, "SCHEDULER_LOCK_FILE", layout.beside(".scheduler.pid"))
    monkeypatch.setattr(cfg, "SCHEDULER_RELOAD_FLAG", layout.beside(".scheduler_reload.flag"))
    monkeypatch.setattr(cfg, "SCHEDULER_FIRE_CLAIM_DIR", layout.beside(".fire_claims"))
    monkeypatch.setattr(cfg, "PROGRESS_FILE", layout.beside(".progress.json"))
    monkeypatch.setattr(cfg, "STREAM_COUNT_LEDGER_FILE", str(layout.data_dir + "/stream_counts.jsonl"))
    legacy = dict(cfg.LEGACY_PATHS)
    legacy.update({
        "SCHEDULER_LOCK_FILE": str(old / "iptv_checker_scheduler.pid"),
        "SCHEDULER_RELOAD_FLAG": str(old / "iptv_checker_scheduler_reload.flag"),
        "SCHEDULER_FIRE_CLAIM_DIR": str(old / "iptv_checker_fire_claims"),
        "PROGRESS_FILE": str(old / "iptv_checker_progress.json"),
        "STREAM_COUNT_LEDGER_FILE": str(old / "iptv_checker_stream_counts.jsonl"),
    })
    monkeypatch.setattr(cfg, "LEGACY_PATHS", legacy)
    os.makedirs(layout.data_dir, exist_ok=True)
    return types.SimpleNamespace(layout=layout, old=old, cfg=cfg)


@pytest.fixture
def foreign_process():
    """A live process that is not this test process, for an owner record that must block us."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _record(pid, token=""):
    return "%d\n%s" % (pid, token)


# --- Scheduler owner record ---------------------------------------------------------------------

@PosixOnly
def test_a_live_old_owner_record_blocks_the_new_election(rollout, plugin, foreign_process):
    old_record = rollout.old / "iptv_checker_scheduler.pid"
    old_record.write_text(_record(foreign_process.pid), encoding="utf-8")
    assert plugin._acquire_scheduler_lock() is False
    assert not os.path.exists(rollout.cfg.SCHEDULER_LOCK_FILE), "no new record for a second loop"
    assert old_record.read_text(encoding="utf-8").startswith(str(foreign_process.pid))


@PosixOnly
def test_a_new_election_takes_the_old_record_and_the_new_one(rollout, plugin):
    assert plugin._acquire_scheduler_lock() is True
    me = str(os.getpid())
    assert open(rollout.old / "iptv_checker_scheduler.pid", encoding="utf-8").readline().strip() == me
    assert open(rollout.cfg.SCHEDULER_LOCK_FILE, encoding="utf-8").readline().strip() == me


@PosixOnly
def test_a_lost_new_election_gives_back_the_old_record(rollout, plugin, foreign_process):
    # The current record names a live process, so the new election is lost after the old one was taken.
    with open(rollout.cfg.SCHEDULER_LOCK_FILE, "w", encoding="utf-8") as fh:
        fh.write(_record(foreign_process.pid))
    assert plugin._acquire_scheduler_lock() is False
    assert not os.path.exists(rollout.old / "iptv_checker_scheduler.pid"), \
        "a lost election must not leave this process as the old-code owner"


@PosixOnly
def test_an_old_record_naming_another_live_process_supersedes_this_loop(rollout, plugin, foreign_process):
    assert plugin._acquire_scheduler_lock() is True
    assert plugin._scheduler_lock_taken_over() is False
    (rollout.old / "iptv_checker_scheduler.pid").write_text(_record(foreign_process.pid), encoding="utf-8")
    assert plugin._scheduler_lock_taken_over() is True


def test_both_records_naming_this_process_do_not_supersede_it(rollout, plugin):
    me = _record(os.getpid())
    (rollout.old / "iptv_checker_scheduler.pid").write_text(me, encoding="utf-8")
    with open(rollout.cfg.SCHEDULER_LOCK_FILE, "w", encoding="utf-8") as fh:
        fh.write(me)
    assert plugin._scheduler_lock_taken_over() is False


def test_the_owner_record_path_is_only_the_new_one_when_rollout_is_off(rollout, plugin, monkeypatch):
    monkeypatch.setattr(storage, "ROLLOUT_DUAL_PATHS", False)
    assert pm._state_paths("SCHEDULER_LOCK_FILE") == [rollout.cfg.SCHEDULER_LOCK_FILE]


# --- Fire claims ----------------------------------------------------------------------------------

def test_a_fire_claim_is_written_in_the_old_folder_and_the_new_one(rollout, plugin):
    cron = "7 7 * * 1 rollout-both"
    assert plugin._claim_scheduler_fire(cron, MINUTE) is True
    name = os.path.basename(pm.Plugin._fire_claim_path(cron, MINUTE, str(rollout.old / "iptv_checker_fire_claims")))
    assert os.path.exists(rollout.old / "iptv_checker_fire_claims" / name)
    assert os.path.exists(os.path.join(rollout.cfg.SCHEDULER_FIRE_CLAIM_DIR, name))


def test_a_claim_made_by_old_code_stops_the_new_loop(rollout, plugin):
    cron = "7 7 * * 2 old-claim"
    old_dir = rollout.old / "iptv_checker_fire_claims"
    old_dir.mkdir()
    path = pm.Plugin._fire_claim_path(cron, MINUTE, str(old_dir))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("1\n")
    assert plugin._claim_scheduler_fire(cron, MINUTE) is False
    assert not os.path.exists(os.path.join(rollout.cfg.SCHEDULER_FIRE_CLAIM_DIR,
                                           os.path.basename(path))), "the old folder is checked first"


def test_a_claim_made_by_new_code_stops_an_old_code_claim(rollout, plugin):
    cron = "7 7 * * 3 new-claim"
    assert plugin._claim_scheduler_fire(cron, MINUTE) is True
    old_path = pm.Plugin._fire_claim_path(cron, MINUTE, str(rollout.old / "iptv_checker_fire_claims"))
    # Old code opens its claim with O_CREAT|O_EXCL at the old path, exactly as this does.
    with pytest.raises(FileExistsError):
        os.open(old_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)


def test_a_second_claim_for_the_same_minute_is_refused(rollout, plugin):
    cron = "7 7 * * 4 twice"
    assert plugin._claim_scheduler_fire(cron, MINUTE) is True
    assert plugin._claim_scheduler_fire(cron, MINUTE) is False


def test_the_claim_folder_is_the_only_one_written_when_rollout_is_off(rollout, plugin, monkeypatch):
    monkeypatch.setattr(storage, "ROLLOUT_DUAL_PATHS", False)
    cron = "7 7 * * 5 single"
    assert plugin._claim_scheduler_fire(cron, MINUTE) is True
    assert not (rollout.old / "iptv_checker_fire_claims").exists()


# --- Reload flag ----------------------------------------------------------------------------------

def test_a_reload_request_is_written_at_both_paths(rollout, plugin):
    plugin._request_scheduler_reload()
    assert os.path.exists(rollout.cfg.SCHEDULER_RELOAD_FLAG)
    assert os.path.exists(os.path.join(str(rollout.old), "iptv_checker_scheduler_reload.flag"))


def test_a_reload_request_is_written_at_one_path_when_rollout_is_off(rollout, plugin, monkeypatch):
    monkeypatch.setattr(storage, "ROLLOUT_DUAL_PATHS", False)
    plugin._request_scheduler_reload()
    assert os.path.exists(rollout.cfg.SCHEDULER_RELOAD_FLAG)
    assert not os.path.exists(rollout.old / "iptv_checker_scheduler_reload.flag")


# --- Progress file mirror -------------------------------------------------------------------------

def test_the_progress_file_is_written_to_both_paths_with_the_same_bytes(rollout, plugin):
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    plugin._save_progress_file({"status": "running", "current": 3, "total": 9}, indent=2)
    new_bytes = open(rollout.cfg.PROGRESS_FILE, "rb").read()
    old_bytes = open(rollout.old / "iptv_checker_progress.json", "rb").read()
    assert new_bytes and new_bytes == old_bytes
    assert b'"status": "running"' in old_bytes


def test_the_progress_mirror_is_absent_when_rollout_is_off(rollout, plugin, monkeypatch):
    monkeypatch.setattr(storage, "ROLLOUT_DUAL_PATHS", False)
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    plugin._save_progress_file({"status": "idle"})
    assert os.path.exists(rollout.cfg.PROGRESS_FILE)
    assert not (rollout.old / "iptv_checker_progress.json").exists()


def test_the_progress_mirror_is_absent_in_legacy_mode(plugin, tmp_path, monkeypatch):
    legacy_layout = storage.Layout(True, None, None, "iptv_checker", "test")
    monkeypatch.setattr(pm.PluginConfig, "STORAGE_LAYOUT", legacy_layout)
    own = tmp_path / "only.json"
    plugin.progress_file = str(own)
    plugin._save_progress_file({"status": "idle"})
    assert own.exists()


def test_the_normalise_on_startup_writes_the_mirror_too(rollout, plugin):
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    with open(rollout.cfg.PROGRESS_FILE, "w", encoding="utf-8") as fh:
        fh.write('{"status": "running", "boot_token": "old-container"}')
    plugin._normalize_stale_progress()
    assert os.path.exists(rollout.old / "iptv_checker_progress.json")
    assert open(rollout.old / "iptv_checker_progress.json", encoding="utf-8").read() == \
        open(rollout.cfg.PROGRESS_FILE, encoding="utf-8").read()


# --- Stream tally ---------------------------------------------------------------------------------

def test_a_tally_append_first_copies_the_old_ledger_tail(rollout, plugin, monkeypatch):
    monkeypatch.setattr(pm.Plugin, "_report_usage", lambda self, *a, **k: None)
    old_ledger = rollout.old / "iptv_checker_stream_counts.jsonl"
    new_ledger = rollout.cfg.STREAM_COUNT_LEDGER_FILE
    old_ledger.write_text('{"streams": 1}\n', encoding="utf-8")
    # The carry-over copied the first line and recorded how far it read the old file.
    item = storage.Item("STREAM_COUNT_LEDGER_FILE", str(old_ledger), new_ledger, "tally", "stream_counts.jsonl")
    storage._ps._copy_tally(storage.SPEC, item, rollout.layout.data_dir,
                            [str(old_ledger)], [])
    # An old-code worker appends a line after the copy.
    with open(old_ledger, "a", encoding="utf-8") as fh:
        fh.write('{"streams": 2}\n')
    plugin._record_streams_checked(5, "scheduled", LOG)
    lines = open(new_ledger, encoding="utf-8").read().splitlines()
    assert lines[0] == '{"streams": 1}'
    assert lines[1] == '{"streams": 2}', "the old-code line is copied before the new one"
    assert '"streams": 5' in lines[2]
    assert len(lines) == 3, "no line is counted twice"


# --- Startup normalise at both progress paths -----------------------------------------------------

def _write_progress_record(path, status, token, age_seconds=0):
    data = {"current": 418, "total": 2691, "status": status, "start_time": time.time() - 600}
    if token:
        data["boot_token"] = token
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))


def _status_at(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["status"]


@pytest.fixture
def this_boot(monkeypatch):
    monkeypatch.setattr(pm, "_container_boot_token", lambda: "thisboot:100")


def test_a_stale_running_record_at_the_old_path_is_reset(rollout, plugin, this_boot):
    """A file the previous release left at the old /data path must not keep saying running."""
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    old = rollout.old / "iptv_checker_progress.json"
    _write_progress_record(old, "running", "oldboot:99")
    plugin._normalize_stale_progress()
    assert _status_at(old) == "idle"


def test_both_stale_running_records_are_reset(rollout, plugin, this_boot):
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    old = rollout.old / "iptv_checker_progress.json"
    _write_progress_record(old, "running", "oldboot:99")
    _write_progress_record(rollout.cfg.PROGRESS_FILE, "running", "oldboot:99")
    plugin._normalize_stale_progress()
    assert _status_at(old) == "idle"
    assert _status_at(rollout.cfg.PROGRESS_FILE) == "idle"


def test_a_live_run_at_the_old_path_is_left_alone(rollout, plugin, this_boot):
    """An old-code worker still checking: fresh, stamped by this container, so not debris."""
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    old = rollout.old / "iptv_checker_progress.json"
    _write_progress_record(old, "running", "thisboot:100", age_seconds=0)
    plugin._normalize_stale_progress()
    assert _status_at(old) == "running"


def test_the_old_path_is_not_normalised_when_rollout_is_off(rollout, plugin, this_boot, monkeypatch):
    monkeypatch.setattr(storage, "ROLLOUT_DUAL_PATHS", False)
    plugin.progress_file = rollout.cfg.PROGRESS_FILE
    old = rollout.old / "iptv_checker_progress.json"
    _write_progress_record(old, "running", "oldboot:99")
    plugin._normalize_stale_progress()
    assert _status_at(old) == "running"
