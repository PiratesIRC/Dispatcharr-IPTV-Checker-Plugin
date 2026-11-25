"""IPTV Checker's storage: where its private state lives and what moves over from /data.

The logic (resolving the data directory, carrying the old /data state over, the tally tail and
binding the state constants) is in plugin_storage.py, a vendored copy of _shared/plugin_storage.py.
This module holds only what belongs to this plugin: the Spec, the table of state paths, the names
kept beside the data directories and the rollout switch. Each public function passes SPEC to the
vendored module, so callers see the same names they always did.
"""
from __future__ import annotations

try:
    from . import plugin_storage as _ps
except ImportError:  # loaded as a bare module, as plugin.py and tests/conftest.py allow
    import plugin_storage as _ps

# Rollout switch for this release only: remove in the release after next, with the dual-path
# branches it guards (plugin.py _rollout_old_path and its callers) and the tests that name it.
# While it is on, a worker on this code also honours the old /data files that workers on the
# previous release still use: the scheduler owner record, the reload flag and the fire claims.
# It also keeps the progress file mirrored at its old /data path, because Stream-Mapparr reads
# that file to stand aside while a scan runs.
ROLLOUT_DUAL_PATHS = True

# (constant name, kind, rel under the data dir). rel is None for the rows that live beside the
# data dirs or in the shared reports dir; those are named in _BESIDE. The settings file is a
# cache of the PluginConfig.settings row, which does not move (design rule 9); this plugin keeps
# its settings in the row only, so no settings file is listed.
_BIND_TABLE = (
    ("RESULTS_FILE", "file", "results.json"),
    ("LOADED_CHANNELS_FILE", "file", "loaded_channels.json"),
    ("PENDING_RESUME_FILE", "file", "pending_resume.json"),
    ("CHANNEL_STATE_FILE", "file", "channel_state.json"),
    ("STREAM_COUNT_LEDGER_FILE", "tally", "stream_counts.jsonl"),
    ("USAGE_DATA_DIR", "usage_dir", "usage"),
    ("PROGRESS_FILE", "nocopy", None),
    ("SCHEDULER_LOCK_FILE", "nocopy", None),
    ("SCHEDULER_RELOAD_FLAG", "nocopy", None),
    ("SCHEDULER_FIRE_CLAIM_DIR", "nocopy", None),
    ("REPORT_FALLBACK_DIR", "nocopy", None),
)

# Names beside the data directories, in <plugins>/_data/<PLUGIN_DB_KEY><suffix>. They use the
# folder key, not the Hub slug, so every worker computes the same path whatever slug it resolved
# (design rule 7). The progress file is named by the released Stream-Mapparr's stand-aside reader
# only through its old /data path; see PluginConfig.PROGRESS_FILE.
_BESIDE = {
    "PROGRESS_FILE": ".progress.json",
    "SCHEDULER_LOCK_FILE": ".scheduler.pid",
    "SCHEDULER_RELOAD_FLAG": ".scheduler_reload.flag",
    "SCHEDULER_FIRE_CLAIM_DIR": ".fire_claims",
}

# The shared reports folder is the one Newsflasharr reads; reports go there only when the
# fallback folder is used, because the report path is attached to a Newsflasharr event.
SPEC = _ps.Spec(
    label="IPTV Checker",
    state_name="_iptv_checker_storage_state",
    table=_BIND_TABLE,
    beside=_BESIDE,
    reports_subdir="iptv-checker",
    usage_subdir="iptv_checker",
    tally_offsets="stream_counts.offsets.json",
    tally_lock="stream_counts.lock",
    reports_constant="REPORT_FALLBACK_DIR",
)

Layout = _ps.Layout
Item = _ps.Item
MigrateResult = _ps.MigrateResult
atomic_write_bytes = _ps.atomic_write_bytes
atomic_write_json = _ps.atomic_write_json


def resolve(plugin_dir, key, logger):
    """Resolve the data directory for the plugin folder plugin_dir. See plugin_storage.resolve."""
    return _ps.resolve(SPEC, plugin_dir, key, logger)


def migrate(layout, items, logger, version=None):
    """Copy the old state into the data dir once. See plugin_storage.migrate."""
    return _ps.migrate(SPEC, layout, items, logger, version=version)


def sync_tally_tail(layout, tally_path, logger):
    """Append the tally's new old-source bytes to it. See plugin_storage.sync_tally_tail."""
    return _ps.sync_tally_tail(SPEC, layout, tally_path, logger)


def items_for(cfg, layout):
    """The moves bind hands to migrate. See plugin_storage.items_for."""
    return _ps.items_for(SPEC, cfg, layout)


def bind(cfg, plugin_file, logger):
    """Resolve, rebind the state constants and carry the old state over. See plugin_storage.bind."""
    return _ps.bind(SPEC, cfg, plugin_file, logger)


def check_context(layout, context, logger):
    """Warn once when Dispatcharr's data_dir differs from the resolved one. See plugin_storage."""
    return _ps.check_context(SPEC, layout, context, logger)
