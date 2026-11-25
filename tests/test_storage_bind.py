"""Binding the state paths to the plugin data directory, and carrying the old /data state over.

A non-legacy layout is faked by patching the one lookup that finds Dispatcharr's data directory
(plugin_storage._from_module_context). Everything else runs as it does in the container.
"""
import logging
import os
import types

import pytest

import iptv_checker.plugin as pm
import iptv_checker.storage as storage

LOG = logging.getLogger("iptv_checker.tests.storage_bind")


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append((record.levelno, record.getMessage()))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A plugins folder with the plugin at plugins/iptv_checker and its data dir under plugins/_data."""
    plugins = tmp_path / "plugins"
    plugin_dir = plugins / "iptv_checker"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("# test\n", encoding="utf-8")
    data = plugins / "_data" / "iptv_checker"
    monkeypatch.setattr(storage._ps, "_from_module_context", lambda plugin_dir, failures: (str(data), "test"))
    monkeypatch.setattr(storage._ps, "_from_registry", lambda key, failures: (None, None))
    old = tmp_path / "old"
    old.mkdir()
    legacy = {name: str(old / ("%s.literal" % name)) for name, _kind, _rel in storage.SPEC.table}
    legacy["USAGE_DATA_DIR"] = str(old / "plugin_stats")
    legacy["REPORT_FALLBACK_DIR"] = str(old / "reports")
    cfg = types.SimpleNamespace(PLUGIN_DB_KEY="iptv_checker", PLUGIN_VERSION="test", **legacy)
    return types.SimpleNamespace(plugins=plugins, plugin_file=str(plugin_dir / "plugin.py"), data=data,
                                 old=old, cfg=cfg, legacy=legacy)


def _bind(env, logger=LOG):
    return storage.bind(env.cfg, env.plugin_file, logger)


def test_bind_moves_every_constant_into_the_data_dir_or_beside_it(env):
    layout = _bind(env)
    assert layout.legacy is False
    cfg, data, plugins = env.cfg, env.data, env.plugins
    assert cfg.RESULTS_FILE == str(data / "results.json")
    assert cfg.LOADED_CHANNELS_FILE == str(data / "loaded_channels.json")
    assert cfg.PENDING_RESUME_FILE == str(data / "pending_resume.json")
    assert cfg.CHANNEL_STATE_FILE == str(data / "channel_state.json")
    assert cfg.STREAM_COUNT_LEDGER_FILE == str(data / "stream_counts.jsonl")
    assert cfg.USAGE_DATA_DIR == str(data / "usage")
    # Beside the data dirs, under the folder key (never the Hub slug).
    assert cfg.PROGRESS_FILE == str(plugins / "_data" / "iptv_checker.progress.json")
    assert cfg.SCHEDULER_LOCK_FILE == str(plugins / "_data" / "iptv_checker.scheduler.pid")
    assert cfg.SCHEDULER_RELOAD_FLAG == str(plugins / "_data" / "iptv_checker.scheduler_reload.flag")
    assert cfg.SCHEDULER_FIRE_CLAIM_DIR == str(plugins / "_data" / "iptv_checker.fire_claims")
    # The fallback report folder is the shared folder Newsflasharr reads.
    assert cfg.REPORT_FALLBACK_DIR == str(plugins / "_shared" / "newsflasharr" / "iptv-checker")


def test_legacy_literals_are_recorded_before_the_rebind(env):
    _bind(env)
    assert env.cfg.LEGACY_PATHS["RESULTS_FILE"] == env.legacy["RESULTS_FILE"]
    assert env.cfg.LEGACY_PATHS["SCHEDULER_LOCK_FILE"] == env.legacy["SCHEDULER_LOCK_FILE"]


def test_results_are_carried_over_and_an_existing_new_file_wins(env):
    (env.old / "RESULTS_FILE.literal").write_text('{"old": 1}', encoding="utf-8")
    _bind(env)
    assert (env.data / "results.json").read_text(encoding="utf-8") == '{"old": 1}'
    assert (env.old / "RESULTS_FILE.literal").exists(), "the old file is never removed"
    # A second process finds the marker and copies nothing more.
    (env.data / "results.json").write_text('{"new": 2}', encoding="utf-8")
    storage._ps._state(storage.SPEC).migrated_dirs.clear()
    _bind(env)
    assert (env.data / "results.json").read_text(encoding="utf-8") == '{"new": 2}'


def test_a_finished_pass_writes_the_markers_and_migrated_json(env):
    (env.old / "CHANNEL_STATE_FILE.literal").write_text("{}", encoding="utf-8")
    _bind(env)
    assert (env.data / "migrated.json").exists()
    assert (env.plugins / "_data" / "iptv_checker.migrated").exists()
    assert (env.plugins / "_data" / "iptv_checker.active").exists()


def test_old_reports_are_not_copied_to_the_shared_folder(env):
    (env.old / "reports").mkdir()
    (env.old / "reports" / "report.html").write_text("<html></html>", encoding="utf-8")
    _bind(env)
    shared = env.plugins / "_shared" / "newsflasharr" / "iptv-checker"
    assert not (shared / "report.html").exists()


def test_the_usage_install_id_moves_into_the_usage_folder(env):
    old_install = env.old / "plugin_stats" / "iptv_checker"
    old_install.mkdir(parents=True)
    (old_install / "install_id").write_text("abc123", encoding="utf-8")
    _bind(env)
    moved = env.data / "usage" / "iptv_checker" / "install_id"
    assert moved.read_text(encoding="utf-8") == "abc123"
    assert (old_install / "install_id").exists()


def test_a_failed_install_id_copy_is_reported_as_blocked(env, monkeypatch):
    old_install = env.old / "plugin_stats" / "iptv_checker"
    old_install.mkdir(parents=True)
    (old_install / "install_id").write_text("abc123", encoding="utf-8")
    real_copy = storage._ps._copy_one

    def failing_copy(dst, sources, copied):
        if dst.endswith("install_id"):
            raise OSError("disk full")
        return real_copy(dst, sources, copied)

    monkeypatch.setattr(storage._ps, "_copy_one", failing_copy)
    _bind(env)
    migration = env.cfg.STORAGE_MIGRATION
    assert migration.install_id_blocked is True
    assert "install_id" in migration.failed


def test_a_blocked_install_id_silences_the_usage_reporter():
    calls = []
    reporter = types.SimpleNamespace(report=lambda *a, **k: calls.append(a))
    blocked = types.SimpleNamespace(install_id_blocked=True)
    pm._silence_usage_if_install_id_blocked(reporter, blocked).report({"x": 1})
    assert calls == []


def test_an_unblocked_install_id_leaves_the_usage_reporter_alone():
    calls = []
    reporter = types.SimpleNamespace(report=lambda *a, **k: calls.append(a))
    clear = types.SimpleNamespace(install_id_blocked=False)
    pm._silence_usage_if_install_id_blocked(reporter, clear).report({"x": 1})
    assert calls == [({"x": 1},)]
    pm._silence_usage_if_install_id_blocked(reporter, None).report({"y": 2})
    assert calls[-1] == ({"y": 2},)


def test_without_a_data_directory_the_constants_keep_their_legacy_values(monkeypatch, tmp_path):
    monkeypatch.setattr(storage._ps, "_from_module_context", lambda plugin_dir, failures: (None, None))
    monkeypatch.setattr(storage._ps, "_from_registry", lambda key, failures: (None, None))
    monkeypatch.setattr(storage._ps, "_sandbox_problem", lambda: ImportError("no sandbox"))
    cfg = types.SimpleNamespace(PLUGIN_DB_KEY="iptv_checker", PLUGIN_VERSION="test",
                                RESULTS_FILE="/data/iptv_checker_results.json")
    layout = storage.bind(cfg, str(tmp_path / "plugin.py"), LOG)
    assert layout.legacy is True
    assert cfg.RESULTS_FILE == "/data/iptv_checker_results.json"
    assert cfg.STORAGE_MIGRATION is None


def test_a_path_outside_the_plugins_data_folder_is_refused(tmp_path):
    plugins = str(tmp_path / "plugins")
    problem = storage._ps._guard(str(tmp_path / "elsewhere" / "iptv_checker"), plugins)
    assert problem == "path is not directly under <plugins>/_data"


def test_context_data_dir_mismatch_is_warned_once():
    layout = storage.Layout(False, "/x/plugins/_data/iptv_checker", "/x/plugins", "iptv_checker", "test")
    rec = _Recorder()
    log = logging.getLogger("iptv_checker.tests.context")
    log.addHandler(rec)
    try:
        ctx = {"data_dir": "/x/plugins/_data/iptv-checker"}
        storage.check_context(layout, ctx, log)
        storage.check_context(layout, ctx, log)
    finally:
        log.removeHandler(rec)
    warnings = [m for level, m in rec.messages if level == logging.WARNING]
    assert len(warnings) == 1
    assert "iptv-checker" in warnings[0]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink not available")
def test_a_symlinked_data_root_is_refused(tmp_path):
    plugins = tmp_path / "plugins"
    real = tmp_path / "real"
    real.mkdir()
    (plugins).mkdir()
    link = plugins / "_data"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError:
        pytest.skip("creating a symlink needs privileges on this host")
    problem = storage._ps._guard(str(link / "iptv_checker"), str(plugins))
    assert problem == "path or its root is a symlink"
