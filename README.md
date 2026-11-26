# Dispatcharr IPTV Checker Plugin

## Check IPTV stream status, analyze stream quality, and manage channels based on results

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
[![Workflow Guide](https://img.shields.io/badge/%F0%9F%93%96-Workflow_Guide-1F6FEB?style=flat)](https://piratesirc.github.io/Dispatcharr-Plugin-Workflow/workflow/01-iptv-checker/)
[![Discord](https://img.shields.io/badge/Discord-Discussion-5865F2?logo=discord&logoColor=white)](https://discord.gg/Sp45V5BcxU)
[![Sponsor](https://img.shields.io/badge/Sponsor-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/PiratesIRC)

[![GitHub Release](https://img.shields.io/github/v/release/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin?include_prereleases&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)
[![Downloads](https://img.shields.io/github/downloads/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/total?color=success&label=Downloads&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)
[![Streams Checked](https://img.shields.io/endpoint?url=https://plugin-stats.dpas.workers.dev/badge/iptv_checker/streams_checked)](#anonymous-usage-counts)
[![Active Installs](https://img.shields.io/endpoint?url=https://plugin-stats.dpas.workers.dev/badge/iptv_checker/installs)](#anonymous-usage-counts)

![Top Language](https://img.shields.io/github/languages/top/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![Repo Size](https://img.shields.io/github/repo-size/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![Last Commit](https://img.shields.io/github/last-commit/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
![License](https://img.shields.io/github/license/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)

## Warning: back up your database first

This plugin renames, moves and can permanently delete channels. Before using it,
**[make a backup of your Dispatcharr database](https://dispatcharr.github.io/Dispatcharr-Docs/troubleshooting/?h=backup#how-can-i-make-a-backup-of-the-database)**.

## What it does

Probes every stream behind your channels with `ffprobe`, records what it finds, and lets you act on
the result.

It answers three questions per stream, and keeps them apart because acting on the wrong one
deletes channels that work:

- Alive. The stream plays. Resolution, framerate, codecs and bitrate are recorded and synced
  back into Dispatcharr so the channel menu can show them.
- Dead. The stream does not play, or it plays but shows nothing worth watching: a blank picture,
  a frozen picture, silence, or a fixed-duration placeholder file. Those last four are opt-in. A separate opt-in setting reports Yes or No for closed captions on each stream in the results CSV.
- Skipped. The checker could not judge it. That covers a provider rate-limit response, a
  radio station with no video track, and hosts `ffprobe` cannot read at all. **Skipped is never
  treated as dead**, so nothing destructive touches a stream that was merely throttled.

**A channel is judged by all of its streams, never by one of them.** Most channels carry a primary
and one or more backups, and Dispatcharr fails over between them. A channel is only reported dead
when **every** stream failed, so one dead backup never marks a working channel for deletion.

Other things it does:

- Scheduled checks, including overnight windows that pause at a set time and resume where they
  left off on the next window.
- An HTML report written to `/config/iptv_checker/report.html` (or to `/data/plugins/_shared/newsflasharr/iptv-checker/` when `/config` cannot be written), grouped by what you should do
  about each finding, and optionally emailed through the
  [Newsflasharr](https://github.com/PiratesIRC) plugin. It is one self-contained file that fetches
  nothing from the internet, so it reads the same in a mail client and on a television browser, and
  it follows a light or dark theme on its own.
- CSV export with a preamble that says what the run did before it says how it was configured, so
  every run leaves a record. Old exports can be deleted automatically after a number of days you
  choose.
- Rename, move, restore and delete actions. Only deleting is irreversible, and only that button is
  red.
- Self-healing: a channel that comes back to life is renamed back and moved to its original
  group automatically.

## Requirements

- Dispatcharr v0.34.0 or newer, with channels and groups already configured. The plugin uses the
  0.34 plugin manifest, so Dispatcharr 0.33 and earlier cannot install this release; stay on
  v1.26.3121307 there.
- `ffprobe` in the container, and `ffmpeg` as well if you enable blank-screen, frozen-video or
  silent-audio detection.
- `pytz` for the scheduler, which is normally already present.

```bash
docker exec dispatcharr which ffprobe
docker exec dispatcharr which ffmpeg
```

No API credentials are needed. The plugin runs inside Dispatcharr with direct database access.

## Install

1. Log in to Dispatcharr and go to **Plugins**.
2. Click **Import Plugin** and upload the release zip.
3. Enable the plugin.

**To update**, delete the old plugin in the Plugins page, restart the container
(`docker restart dispatcharr`), then import the new zip. Your settings are preserved.

## Quick start

1. Pick groups in **Channel Groups** and set **Channel Groups Mode**. Leave the list empty to check
   every group. Entries such as `US:*` pick every group whose name starts with `US:`.
2. Click **Validate** to confirm the plugin can see your groups. It reports how many groups will be
   checked.
3. Click **Load Groups**, then **Start Check**.
4. Click **View Status** to follow the check. It shows progress while the check runs and the last results once it finishes. **Email Report** sends the full report.

## Documentation

**[Full user guide](docs/USER-GUIDE.md)** covers every setting and button, the detection modes,
scheduling and windowed runs, the HTML report and email delivery, and troubleshooting.

- [Development workflow](DEVELOPMENT.md)
- [Release notes](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)

## Versioning

Calver `1.26.{DDD}{HHMM}`, being the UTC day-of-year and UTC hour-minute, matching the Lineuparr,
Channel-Mapparr and EPG-Janitor plugins. Releases before `1.26.1081815` used semver.

## Contributing

Issues and pull requests are welcome. When reporting a problem, please include your Dispatcharr
version, the relevant container logs
(`docker logs dispatcharr | grep "IPTV Checker"`), and the exact error text.

Contribution steps, the CI gates, and how updates reach the Dispatcharr plugin marketplace are in
[DEVELOPMENT.md](DEVELOPMENT.md).

## Disclaimer

This plugin makes bulk changes to your channel database, including permanent deletion when you
enable it. Test on a small group first, keep a database backup, and read what an action says it will
do before confirming it.

## Where IPTV Checker keeps its files

The plugin's private state is kept in a data folder that Dispatcharr creates for it under
`/data/plugins/_data/`. The folder is named after the install: `iptv_checker` for a folder install and
`iptv-checker` for an install from the Plugin Hub. In this guide `<data folder>` means that folder.

| What | Where |
|---|---|
| Last results, loaded channels, windowed resume state, original groups for Restore | `<data folder>` (`results.json`, `loaded_channels.json`, `pending_resume.json`, `channel_state.json`) |
| Streams-checked tally and the record of how far it was copied | `<data folder>` (`stream_counts.jsonl`, `stream_counts.offsets.json`) |
| Anonymous usage install id and timing | `<data folder>/usage/iptv_checker/` |
| Scheduler owner record, reload flag, fired-minute claims | Beside the data folder: `/data/plugins/_data/iptv_checker.scheduler.pid`, `iptv_checker.scheduler_reload.flag` and the folder `iptv_checker.fire_claims/` |
| Progress file | Beside the data folder: `/data/plugins/_data/iptv_checker.progress.json` |
| Report fallback, used only when `/config` cannot be written | `/data/plugins/_shared/newsflasharr/iptv-checker/` (the folder Newsflasharr reads) |
| HTML report, dated copies and report CSV | `/config/iptv_checker/` (unchanged) |
| CSV exports | `/data/exports/` (unchanged, shared with other plugins) |

The files beside the data folder always use the folder key `iptv_checker`, whatever the install route,
so every Dispatcharr worker finds the same owner record and claims. "Delete data" on uninstall removes
the data folder only. The files beside it and the report folder are left in place.

**Moving from the old layout.** The first start of this release copies each old `/data/iptv_checker_*`
file into its new place. It only copies: a file that already exists in the new place is kept, and no old
file is deleted or changed. While this release keeps the old layout, a worker on this release also reads
the old owner record, reload flag and fire claims, so a worker still running the previous release does
not start a second scheduler or fire a minute twice. The carry-over record is `migrated.json` in the data
folder. Its `failed` list should be empty.

**Progress mirror.** While the old layout is kept, every progress write also goes to
`/data/iptv_checker_progress.json`, because Stream-Mapparr reads that exact path to stand aside while a
scan runs. Point Stream-Mapparr at `/data/plugins/_data/iptv_checker.progress.json` and remove the mirror in
the release after this one.

**When the old files are safe to delete.** Only when `migrated.json` has an empty `failed` list and no old
file has changed since the `time` recorded in it. An old file that keeps changing means a worker still runs
the previous release.

## Anonymous usage counts

The Streams Checked and Active Installs badges count every install that leaves
the "Share anonymous usage counts" setting (on by default) ticked and runs at
least one action or check. When an action or check finishes, at most once an
hour, or ten minutes after the last report when a check has just checked
streams, the plugin sends this plugin's Streams Checked total and a random id
for this plugin on this install to the plugin author's counter at
plugin-stats.dpas.workers.dev. The server stores that id with the total and the
date of the last report. The connection shows the server your public IP
address; the server uses it only to limit abuse and does not store it in its
database, though when an install first registers it keeps a salted one-way hash
of it (of its /64 block for IPv6) for up to three days. Cloudflare, which hosts
the server, keeps its own request logs. No names, channels, streams, URLs,
providers or settings are sent. The figures are self-reported by installs and
capped by the server, not verified. Untick the setting to stop sending; this
install's figures are deleted from the server the next time an action or check
finishes after you untick. Details: [User Guide](docs/USER-GUIDE.md#anonymous-usage-counts).

## License

MIT. See [LICENSE](LICENSE).
