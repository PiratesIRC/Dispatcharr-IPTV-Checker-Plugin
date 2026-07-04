"""Scheduler host-eligibility filter (GitHub #25).

On native (non-Docker) Dispatcharr installs running uwsgi + daphne, the daphne
ASGI process could win the O_EXCL scheduler election but never bring a live
scheduler loop up (it acquires the lock during a boot import where the DB-backed
schedule isn't yet readable, then holds it). Every uwsgi worker then deferred to
daphne forever and the cron never fired. The election now excludes non-worker
processes (daphne/ASGI) from candidacy so a uwsgi worker always hosts the
scheduler. Reporter: mcortt.
"""
import os


def test_daphne_cmdline_is_ineligible(pmod):
    cmd = ("/opt/dispatcharr/.venv/bin/python3 /opt/dispatcharr/.venv/bin/daphne "
           "-b 0.0.0.0 -p 8001 dispatcharr.asgi:app")
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is True


def test_asgi_module_cmdline_is_ineligible(pmod):
    # Some installs launch the ASGI app without the literal "daphne" token.
    cmd = "python -m uvicorn dispatcharr.asgi:app --host 0.0.0.0 --port 8001"
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is True


def test_uwsgi_worker_cmdline_is_eligible(pmod):
    cmd = "uwsgi --ini /opt/dispatcharr/uwsgi.ini"
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is False


def test_path_containing_daphne_does_not_exclude_uwsgi_worker(pmod):
    # QA finding: an install rooted at /home/daphne/... (or a 'daphne' username)
    # must NOT make a uwsgi worker ineligible. A bare substring match would
    # exclude every process there, silently leaving no scheduler host — the same
    # failure #25 fixes. Markers match a command-line token basename, not a raw
    # substring of the whole path.
    cmd = "uwsgi --ini /home/daphne/dispatcharr/uwsgi.ini"
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is False


def test_path_containing_daphne_does_not_exclude_celery(pmod):
    cmd = "/home/daphne/.venv/bin/python -m celery -A dispatcharr worker"
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is False


def test_daphne_executable_still_excluded_under_daphne_path(pmod):
    # The actual daphne binary under that same path is still excluded.
    cmd = "/home/daphne/.venv/bin/daphne -b 0.0.0.0 dispatcharr.asgi:app"
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host(cmd) is True


def test_blank_cmdline_is_eligible_fail_open(pmod):
    # Unknown/unreadable cmdline must NOT exclude the process — otherwise a
    # deployment we can't fingerprint would never host the scheduler at all.
    assert pmod.Plugin._cmdline_is_ineligible_scheduler_host("") is False


def test_is_ineligible_reads_cmdline_and_fails_open(plugin, monkeypatch):
    # Reader failure (non-Linux, no /proc) => empty string => eligible.
    monkeypatch.setattr(plugin, "_read_own_cmdline", lambda: "")
    assert plugin._is_ineligible_scheduler_host() is False
    monkeypatch.setattr(plugin, "_read_own_cmdline",
                        lambda: "python daphne dispatcharr.asgi:app")
    assert plugin._is_ineligible_scheduler_host() is True


def test_ineligible_host_cannot_win_election(plugin, pmod, monkeypatch, tmp_path):
    """A daphne/ASGI process must never acquire or create the election lock."""
    lock_file = str(tmp_path / "sched.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(plugin, "_is_ineligible_scheduler_host", lambda: True)
    assert plugin._acquire_scheduler_lock() is False
    assert not os.path.exists(lock_file), "ineligible host must not create the lock file"


def test_eligible_host_still_wins_on_empty_slot(plugin, pmod, monkeypatch, tmp_path):
    """Guard against over-exclusion: an eligible (uwsgi) process still elects."""
    lock_file = str(tmp_path / "sched.pid")
    monkeypatch.setattr(pmod.PluginConfig, "SCHEDULER_LOCK_FILE", lock_file)
    monkeypatch.setattr(pmod, "_container_boot_token", lambda: "newboot:200")
    monkeypatch.setattr(plugin, "_is_ineligible_scheduler_host", lambda: False)
    assert plugin._acquire_scheduler_lock() is True
    assert os.path.exists(lock_file)
