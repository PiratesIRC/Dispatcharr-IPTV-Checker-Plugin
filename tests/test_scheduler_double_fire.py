"""Regression tests for the 2026-07-03 scheduler double-fire and the GitHub #25
"Check Scheduler Status reports Not running" false negative.

Fix #1 (double-fire): the per-minute "already ran" guard used to be a
`scheduler_loop`-local `last_run` dict. When a lifecycle race left two loop
threads alive in the elected process, each kept its own `last_run`, so both
fired the same cron minute — on 2026-07-03 '0 23 * * *' fired twice 4s apart
from the same owner PID 256 and produced two overlapping CSVs. The fire-claim
is now process-shared and lock-guarded (`_claim_scheduler_fire`): only the
first claimant of a (cron_expr, minute) fires.

Fix #2 (status false negative): `check_scheduler_status_action` used to read
the process-local `_bg_scheduler_thread`, which is None in every worker except
the one hosting the scheduler (often daphne). A UI status click lands in some
other worker, so it always reported "Not running". It now consults the
cross-process election lock file: a live holder PID means the scheduler is
running somewhere.
"""
import threading

import pytest


@pytest.fixture(autouse=True)
def _reset_fire_guard(pmod):
    """Isolate the process-shared fire marker between tests."""
    pmod._scheduler_last_fired.clear()
    yield
    pmod._scheduler_last_fired.clear()


# --------------------------- Fix #1: fire-claim guard ---------------------------

def test_claim_fires_once_per_minute(plugin, pmod):
    minute = "2026-07-03T04:00"  # opaque marker; equality is all that matters
    assert plugin._claim_scheduler_fire("0 23 * * *", minute) is True
    # Same (expr, minute) — a sibling loop thread — must NOT fire again.
    assert plugin._claim_scheduler_fire("0 23 * * *", minute) is False
    assert plugin._claim_scheduler_fire("0 23 * * *", minute) is False


def test_claim_rearms_next_minute(plugin, pmod):
    assert plugin._claim_scheduler_fire("0 23 * * *", "min-1") is True
    assert plugin._claim_scheduler_fire("0 23 * * *", "min-1") is False
    # A new minute re-arms.
    assert plugin._claim_scheduler_fire("0 23 * * *", "min-2") is True


def test_claim_is_per_cron_expression(plugin, pmod):
    assert plugin._claim_scheduler_fire("0 23 * * *", "m") is True
    # A different cron expression matching the same minute still fires.
    assert plugin._claim_scheduler_fire("30 11 * * *", "m") is True
    assert plugin._claim_scheduler_fire("0 23 * * *", "m") is False


def test_concurrent_claims_exactly_one_wins(plugin, pmod):
    """Models two (or more) scheduler_loop threads racing the same cron minute:
    exactly one claim must succeed."""
    minute = "same-minute"
    n = 32
    barrier = threading.Barrier(n)
    wins = []
    guard = threading.Lock()

    def worker():
        barrier.wait()
        won = plugin._claim_scheduler_fire("0 23 * * *", minute)
        if won:
            with guard:
                wins.append(won)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


# --------------------- Fix #2: cross-process status honesty ---------------------

def _status_settings():
    return {"scheduled_times": "0 23 * * *"}


def _prep_status(plugin, pmod, monkeypatch, tmp_path, my_pid, kill_ok=True):
    lock_file = str(tmp_path / "sched.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(pmod, "_container_boot_token", lambda: "boot:1")
    monkeypatch.setattr(pmod.os, "getpid", lambda: my_pid)

    def fake_kill(pid, sig):
        if not kill_ok:
            raise ProcessLookupError()
        return None

    monkeypatch.setattr(pmod.os, "kill", fake_kill)
    plugin.check_progress = {}
    return lock_file


def test_status_reports_running_when_other_process_owns_lock(plugin, pmod, monkeypatch, tmp_path):
    """GitHub #25: another live process (e.g. daphne PID 256) owns the lock and
    this worker has no local thread — status must still say Running."""
    lock_file = _prep_status(plugin, pmod, monkeypatch, tmp_path, my_pid=999, kill_ok=True)
    with open(lock_file, "w") as f:
        f.write("256\nboot:1")  # live holder, current container token
    monkeypatch.setattr(pmod, "_bg_scheduler_thread", None)

    result = plugin.check_scheduler_status_action(_status_settings(), pmod.LOGGER)
    assert result["status"] == "ok"
    assert "Running" in result["message"]
    assert "256" in result["message"]
    assert "Not running" not in result["message"]


def test_status_reports_not_running_when_holder_dead(plugin, pmod, monkeypatch, tmp_path):
    lock_file = _prep_status(plugin, pmod, monkeypatch, tmp_path, my_pid=999, kill_ok=False)
    with open(lock_file, "w") as f:
        f.write("256\nboot:1")  # holder PID is dead (kill raises)
    monkeypatch.setattr(pmod, "_bg_scheduler_thread", None)

    result = plugin.check_scheduler_status_action(_status_settings(), pmod.LOGGER)
    assert "❌ Not running" in result["message"]


def test_status_reports_not_running_when_no_lock(plugin, pmod, monkeypatch, tmp_path):
    _prep_status(plugin, pmod, monkeypatch, tmp_path, my_pid=999)
    monkeypatch.setattr(pmod, "_bg_scheduler_thread", None)
    result = plugin.check_scheduler_status_action(_status_settings(), pmod.LOGGER)
    assert "❌ Not running" in result["message"]


def test_lock_taken_over_yields_when_other_pid_owns(plugin, pmod, monkeypatch, tmp_path):
    """Layer-2 fire guard: a de-elected process (its PID not in the lock) must
    detect it no longer owns the lock so its scheduler_loop yields instead of
    zombie-firing cron."""
    lock_file = str(tmp_path / "sched.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(pmod.os, "getpid", lambda: 243)

    with open(lock_file, "w") as f:
        f.write("256\nboot:1")  # the real owner is a different PID
    assert plugin._scheduler_lock_taken_over() is True

    with open(lock_file, "w") as f:
        f.write("243\nboot:1")  # we own it
    assert plugin._scheduler_lock_taken_over() is False


def test_lock_taken_over_false_when_lock_missing(plugin, pmod, monkeypatch, tmp_path):
    """A missing/unreadable lock must NOT make the real owner yield on a transient
    read glitch."""
    lock_file = str(tmp_path / "absent.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(pmod.os, "getpid", lambda: 243)
    assert plugin._scheduler_lock_taken_over() is False


def test_status_reports_running_when_local_thread_alive(plugin, pmod, monkeypatch, tmp_path):
    _prep_status(plugin, pmod, monkeypatch, tmp_path, my_pid=256)

    class _AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(pmod, "_bg_scheduler_thread", _AliveThread())
    result = plugin.check_scheduler_status_action(_status_settings(), pmod.LOGGER)
    assert "✅ Running" in result["message"]
    assert "Not running" not in result["message"]
