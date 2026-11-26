"""The stream tally append runs inside the tally lock that sync_tally_tail takes.

Without the lock, an append can land between sync_tally_tail's offset write and its own append,
in the middle of another worker's tail sync. The order is checked with spies because the lock is
an fcntl flock: on Windows it is a no-op, so only the order of calls can be asserted there.
"""
import contextlib

import pytest

import iptv_checker.storage as storage


@pytest.fixture
def ledger(pmod, tmp_path, monkeypatch):
    path = tmp_path / "stream_counts.jsonl"
    monkeypatch.setattr(pmod.PluginConfig, "STREAM_COUNT_LEDGER_FILE", str(path))

    def lines():
        return [raw for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]             if path.exists() else []

    lines.path = path
    return lines


def test_the_ledger_append_runs_inside_the_tally_lock(plugin, pmod, ledger, monkeypatch):
    events = []

    @contextlib.contextmanager
    def spy_lock(layout):
        events.append("lock")
        yield
        events.append("unlock")

    real_open = open
    ledger_path = str(ledger.path)

    def spy_open(path, mode="r", *args, **kwargs):
        if str(path) == ledger_path and "a" in mode:
            events.append("append")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(storage, "tally_lock", spy_lock, raising=False)
    monkeypatch.setattr(storage, "sync_tally_tail", lambda layout, path, logger: 0)
    monkeypatch.setattr(pmod, "open", spy_open, raising=False)

    assert plugin._record_streams_checked(7, "parallel") is True

    assert events == ["lock", "append", "unlock"]
    assert len(ledger()) == 1
