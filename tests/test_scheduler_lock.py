"""Cross-process scheduler election lock (`_acquire_scheduler_lock`).

Regression for the duplicate-fire incident on 2026-06-24: two Dispatcharr worker
processes both won the lock on the same container restart (logs showed two
"Scheduler lock acquired" 3ms apart), so the '0 23 * * *' cron fired twice and
produced two overlapping CSVs. Root cause: the old write-tmp -> os.rename ->
re-read-and-confirm protocol was a TOCTOU, not mutual exclusion — a contender
could read back its own PID before another contender's rename overwrote it, so
more than one process confirmed it won. See buglog bug-sched-double-election.

The fix elects a single owner via os.open(O_CREAT|O_EXCL) and reclaims a stale
lock under an exclusive guard. The concurrency tests rely on POSIX
delete/replace-under-read semantics (Linux is the production target); they are
skipped on Windows, where replacing a file open for reading raises a sharing
violation that doesn't occur in the container.
"""
import os
import threading

import pytest


def _install_patches(plugin, pmod, monkeypatch, tmp_path):
    lock_file = str(tmp_path / "sched.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(pmod, "_container_boot_token", lambda: "newboot:200")
    monkeypatch.setattr(pmod.os, "getpid", lambda: getattr(_install_patches.tl, "pid", 1))
    # Default: all PIDs "alive". Individual tests override os.kill when they need
    # a dead holder. (Fake PIDs don't exist, so the real os.kill is unreliable.)
    monkeypatch.setattr(pmod.os, "kill", lambda pid, sig: None)
    _install_patches.tl = threading.local()
    return lock_file


def _elect(plugin, n):
    """Run one barrier-synchronised election; return the list of winning PIDs."""
    tl = _install_patches.tl
    barrier = threading.Barrier(n)
    results = {}
    guard = threading.Lock()

    def worker(pid):
        tl.pid = pid
        barrier.wait()
        won = plugin._acquire_scheduler_lock()
        with guard:
            results[pid] = won

    threads = [threading.Thread(target=worker, args=(1000 + i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [pid for pid, won in results.items() if won]


def test_single_process_reclaims_stale_lock(plugin, pmod, monkeypatch, tmp_path):
    lock_file = _install_patches(plugin, pmod, monkeypatch, tmp_path)
    _install_patches.tl.pid = 4242
    with open(lock_file, "w") as f:
        f.write("123\noldboot:100")  # previous-container (stale) lock
    assert plugin._acquire_scheduler_lock() is True
    with open(lock_file) as f:
        lines = f.read().splitlines()
    # File stays 2 lines — `pid\n{boot_token}` — so _owns_scheduler_lock / triage hold.
    assert int(lines[0]) == 4242
    assert lines[1] == "newboot:200"
    # The reclaim guard and temp file must not leak.
    assert not os.path.exists(lock_file + ".reclaim")
    assert not os.path.exists(lock_file + ".new.4242")


def test_live_current_owner_is_respected(plugin, pmod, monkeypatch, tmp_path):
    """A lock held by a live process in the *same* container is never stolen."""
    lock_file = _install_patches(plugin, pmod, monkeypatch, tmp_path)
    _install_patches.tl.pid = 999999  # a different PID than the holder
    with open(lock_file, "w") as f:
        f.write("4242\nnewboot:200")  # current token, "alive" holder (os.kill no-op)
    assert plugin._acquire_scheduler_lock() is False


def test_recycled_pid_does_not_double_elect(plugin, pmod, monkeypatch, tmp_path):
    """Root cause of the 2026-07-03 double-fire: after a container restart the OS
    recycles low PIDs, so a previous-container stale lock naming PID 243 collides
    with the NEW container's own PID 243. The `holder_pid == my_pid` shortcut used
    to return True without checking the boot token, so the recycled PID silently
    "already owned" the stale lock while another process reclaimed it — TWO winners,
    two scheduler loops, duplicate cron fires. The shortcut must ignore a lock whose
    token is from a previous container.
    """
    lock_file = _install_patches(plugin, pmod, monkeypatch, tmp_path)
    with open(lock_file, "w") as f:
        f.write("243\noldboot:100")  # previous-container lock, holder PID 243

    # The new container's recycled PID 243 races another new process (256).
    _install_patches.tl.pid = 243
    won_243 = plugin._acquire_scheduler_lock()
    _install_patches.tl.pid = 256
    won_256 = plugin._acquire_scheduler_lock()

    assert [won_243, won_256].count(True) == 1, "exactly one process may win the election"
    # The winner must have stamped the CURRENT container token, replacing the stale one.
    with open(lock_file) as f:
        lines = f.read().splitlines()
    assert lines[1] == "newboot:200"


@pytest.mark.skipif(os.name != "posix", reason="relies on POSIX delete-under-read semantics")
def test_concurrent_reclaim_of_previous_container_lock(plugin, pmod, monkeypatch, tmp_path):
    """Restart scenario: many workers reclaim a previous-container (stale-token)
    lock at once — exactly one must win. This is the 2026-06-24 incident path."""
    lock_file = _install_patches(plugin, pmod, monkeypatch, tmp_path)
    for round_no in range(50):
        with open(lock_file, "w") as f:
            f.write("123\noldboot:100")
        winners = _elect(plugin, n=8)
        assert len(winners) == 1, f"round {round_no}: expected one owner, got {winners}"


@pytest.mark.skipif(os.name != "posix", reason="relies on POSIX delete-under-read semantics")
def test_concurrent_reclaim_of_dead_pid_same_token(plugin, pmod, monkeypatch, tmp_path):
    """Same-container dead-PID reclaim: the holder's token matches but its PID is
    dead, so reclaim runs through the os.kill->ProcessLookupError branch. Still
    exactly one owner. (Different branch than the token-mismatch path above.)"""
    lock_file = _install_patches(plugin, pmod, monkeypatch, tmp_path)
    dead_pid = 555

    def fake_kill(pid, sig):
        if pid == dead_pid:
            raise ProcessLookupError()  # the seeded holder is gone
        return None  # live contenders

    monkeypatch.setattr(pmod.os, "kill", fake_kill)
    for round_no in range(50):
        with open(lock_file, "w") as f:
            f.write(f"{dead_pid}\nnewboot:200")  # current token, dead PID
        winners = _elect(plugin, n=8)
        assert len(winners) == 1, f"round {round_no}: expected one owner, got {winners}"
