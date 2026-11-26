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
