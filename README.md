# Dispatcharr IPTV Checker Plugin

## Check IPTV stream status, analyze stream quality, and manage channels based on results

[![Dispatcharr plugin](https://img.shields.io/badge/Dispatcharr-plugin-8A2BE2)](https://github.com/Dispatcharr/Dispatcharr)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin)
[![Workflow Guide](https://img.shields.io/badge/%F0%9F%93%96-Workflow_Guide-1F6FEB?style=flat)](https://piratesirc.github.io/Dispatcharr-Plugin-Workflow/workflow/01-iptv-checker/)
[![Discord](https://img.shields.io/badge/Discord-Discussion-5865F2?logo=discord&logoColor=white)](https://discord.gg/Sp45V5BcxU)
[![Sponsor](https://img.shields.io/badge/Sponsor-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/PiratesIRC)

[![GitHub Release](https://img.shields.io/github/v/release/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin?include_prereleases&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)
[![Downloads](https://img.shields.io/github/downloads/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/total?color=success&label=Downloads&logo=github)](https://github.com/PiratesIRC/Dispatcharr-IPTV-Checker-Plugin/releases)

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

**It answers three questions per stream**, and keeps them apart because acting on the wrong one
deletes channels that work:

- **Alive.** The stream plays. Resolution, framerate, codecs and bitrate are recorded and synced
  back into Dispatcharr so the channel menu can show them.
- **Dead.** The stream does not play, or it plays but shows nothing worth watching: a blank picture,
  a frozen picture, silence, or a fixed-duration placeholder file. Those last four are opt-in.
- **Skipped.** The checker could not judge it. That covers a provider rate-limit response, a
  radio station with no video track, and hosts `ffprobe` cannot read at all. **Skipped is never
  treated as dead**, so nothing destructive touches a stream that was merely throttled.

**A channel is judged by all of its streams, never by one of them.** Most channels carry a primary
and one or more backups, and Dispatcharr fails over between them. A channel is only reported dead
when **every** stream failed, so one dead backup never marks a working channel for deletion.

Other things it does:

- **Scheduled checks**, including overnight windows that pause at a set time and resume where they
  left off on the next window.
- **An HTML report** written to `/config/iptv_checker/report.html`, grouped by what you should do
  about each finding, and optionally emailed through the
  [Newsflasharr](https://github.com/PiratesIRC) plugin. It is one self-contained file that fetches
  nothing from the internet, so it reads the same in a mail client and on a television browser, and
  it follows a light or dark theme on its own.
- **CSV export** with a full settings preamble, so every run leaves an audit record.
- **Rename, move, restore and delete** actions, each with its own confirmation.
- **Self-healing**: a channel that comes back to life is renamed back and moved to its original
  group automatically.

## Requirements

- Dispatcharr v0.20.0 or newer, with channels and groups already configured.
- **`ffprobe`** in the container. **`ffmpeg`** as well if you enable blank-screen, frozen-video or
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

1. Set **Channel Groups** and **Channel Groups Mode**. Leave the box empty to check every group.
2. Click **Validate** to confirm the plugin can see your groups. It reports how many groups will be
   checked.
3. Click **Load Groups**, then **Start Check**.
4. Watch **View Progress**, then read **View Results** or **Email Report**.

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

## License

MIT. See [LICENSE](LICENSE).
