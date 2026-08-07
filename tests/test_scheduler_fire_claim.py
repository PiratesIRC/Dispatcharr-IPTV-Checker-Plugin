"""Cross-process, cross-import claim on a cron minute (`_claim_scheduler_fire`).

Regression for the duplicate-fire incident on 2026-08-07: the cron expression
'0 23 * * *' fired twice, 1.34 seconds apart, and two complete five-hour scans
ran concurrently and emailed two reports.

Root cause. A plugin discovery pass on 2026-08-06 at 18:05 re-imported the
plugin module inside worker 251, which was already the elected scheduler owner.
That left two scheduler loops alive in one process, holding two SEPARATE module
objects, so all three existing guards passed:

  - the election lock names PID 251 and both loops run in PID 251, so
    _scheduler_lock_taken_over sees no different PID;
  - _scheduler_last_fired is a module-level dict, so each module object has its
    own copy and neither sees the other's claim;
  - _bg_scheduler_thread is module-level too, so the older loop still sees
    itself and never self-evicts.

The claim is therefore recorded on disk with os.open(O_CREAT|O_EXCL), the same
mechanism the election lock uses, which no amount of re-importing can defeat.
Clearing _scheduler_last_fired between two claims is exactly what a re-import
looks like from the claim's point of view, so that is how these tests simulate
it.
"""
import os

import pytest


@pytest.fixture
def claims(plugin, pmod, monkeypatch, tmp_path):
    """Point the claim directory at a temp dir and start from a clean slate."""
    d = str(tmp_path / "fire_claims")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_FIRE_CLAIM_DIR", d)
    pmod._scheduler_last_fired.clear()
    return d


def _minute(pmod, hour=23, minute=0, day=7):
    return pmod.datetime(2026, 8, day, hour, minute, 0)


def test_first_caller_claims_the_minute(plugin, pmod, claims):
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True


def test_second_caller_in_the_same_process_is_refused(plugin, pmod, claims):
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is False


def test_a_re_imported_module_cannot_claim_the_same_minute(plugin, pmod, claims):
    """The 2026-08-07 incident. A second scheduler loop holding a fresh module
    object has an empty _scheduler_last_fired, so the in-process guard cannot
    see the first loop's claim. The on-disk claim must still refuse it."""
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True

    pmod._scheduler_last_fired.clear()  # what a module re-import looks like

    assert plugin._claim_scheduler_fire("0 23 * * *", m) is False


def test_a_later_minute_can_still_fire(plugin, pmod, claims):
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    pmod._scheduler_last_fired.clear()
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, minute=1)) is True


def test_a_different_cron_expression_in_the_same_minute_can_fire(plugin, pmod, claims):
    """Two schedules legitimately due in the same minute must both run."""
    m = _minute(pmod)
    assert plugin._claim_scheduler_fire("0 23 * * *", m) is True
    pmod._scheduler_last_fired.clear()
    assert plugin._claim_scheduler_fire("0 23 * * 1", m) is True


def test_the_claim_file_is_not_world_readable(plugin, pmod, claims, monkeypatch):
    """Same reasoning as the election lock: it records a PID and nothing outside
    the owning user needs to read it. Asserting on the mode ARGUMENT rather than
    on stat() keeps this meaningful on Windows, which has no POSIX mode bits."""
    seen = []
    real_open = pmod.os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        seen.append((str(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(pmod.os, "open", spy)
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True

    created = [(p, m) for p, m in seen if claims in p]
    assert created, "no claim file was created, so this test proves nothing"
    for path, mode in created:
        assert mode & 0o077 == 0, f"{path} created with group/other access: {oct(mode)}"


def test_an_unwritable_claim_directory_fails_OPEN(plugin, pmod, claims, monkeypatch):
    """Never fail closed on a backstop. If the claim cannot be recorded, run the
    schedule anyway and complain: a possible duplicate run beats a scheduler that
    silently stops firing forever."""
    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pmod.os, "open", boom)
    warnings = []
    monkeypatch.setattr(pmod.LOGGER, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod)) is True
    assert warnings, "failing open must be logged, or it is a silent degradation"


def test_stale_claim_files_are_pruned(plugin, pmod, claims):
    """The directory must not grow without bound. A claim from a previous day is
    dead: its minute can never come round again."""
    old = _minute(pmod, day=1)
    assert plugin._claim_scheduler_fire("0 23 * * *", old) is True
    assert len(os.listdir(claims)) == 1

    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, day=7)) is True

    names = os.listdir(claims)
    assert len(names) == 1, f"the stale claim was not pruned: {names}"
    assert "20260807" in names[0]


def test_a_claim_from_a_previous_run_of_the_same_day_is_kept(plugin, pmod, claims):
    """Pruning must not delete a claim that is still doing its job. An earlier
    minute today is exactly what stops a duplicate loop re-firing it."""
    assert plugin._claim_scheduler_fire("0 4 * * *", _minute(pmod, hour=4)) is True
    assert plugin._claim_scheduler_fire("0 23 * * *", _minute(pmod, hour=23)) is True
    assert len(os.listdir(claims)) == 2
