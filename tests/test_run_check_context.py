"""run() hands the action context to storage.check_context, so a data_dir that differs from the
resolved one is reported (one WARNING per process) instead of going unnoticed."""
import iptv_checker.storage as storage


def test_run_passes_the_action_context_to_check_context(plugin, pmod, quiet_logger, monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "check_context",
                        lambda layout, context, logger: calls.append((layout, context, logger)) or True)
    context = {"settings": {}, "logger": quiet_logger, "data_dir": "/data/plugins/_data/other"}

    plugin.run("no_such_action", {}, context)

    assert len(calls) == 1
    layout, seen_context, seen_logger = calls[0]
    assert layout is pmod.PluginConfig.STORAGE_LAYOUT
    assert seen_context is context
    assert seen_logger is quiet_logger


def test_a_check_context_that_raises_leaves_the_progress_record_alone(plugin, pmod, quiet_logger, monkeypatch, tmp_path):
    """run()'s except sets the progress record to idle and saves it. A failure in the new
    check_context must not reach that handler, or a live run's record would be overwritten."""
    progress = tmp_path / "progress.json"
    running = '{"status": "running", "current": 5, "total": 9}'
    progress.write_text(running, encoding="utf-8")
    plugin.progress_file = str(progress)
    plugin.check_progress = {"status": "running", "current": 5, "total": 9}

    def boom(layout, context, logger):
        raise RuntimeError("check_context failed")

    ran = []
    monkeypatch.setattr(storage, "check_context", boom)
    monkeypatch.setattr(plugin, "view_status_action", lambda settings, logger: ran.append(1) or {"status": "ok", "message": "ran"}, raising=False)
    context = {"settings": {}, "logger": quiet_logger, "data_dir": "/x"}

    result = plugin.run("view_status", {}, context)

    assert progress.read_text(encoding="utf-8") == running
    assert ran == [1]
    assert result.get("status") == "ok"
