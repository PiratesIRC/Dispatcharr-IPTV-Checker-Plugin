# IPTV Checker v1.26.1221035 — Release Notes

> Single-fix release. Suppresses the packet-based `video_bitrate` calculation when the probe captured too few packets to produce a trustworthy average.

## TL;DR

Verification of the v1.26.1220951 scheduler fix surfaced an unrelated data-quality issue: the v1.26.1220951 morning run wrote `video_bitrate=22924` kbps for stream 903025 (Destination America HD) into Dispatcharr's `stream_stats`. The `ffprobe_data.packet_count` for that row was **2**. The other four streams sampled in the same check had 301–364 packets and produced believable values (3421–4265 kbps).

Below ~30 packets the per-packet noise dominates the average and the bitrate estimate becomes meaningless. This release adds a minimum-sample guard so degenerate probes leave `video_bitrate` unset rather than overwrite Dispatcharr's `stream_stats` with a misleading number. The next probe gets a fresh shot at producing a real value.

## What was actually wrong

`check_stream` falls back to a packet-based bitrate calc when ffprobe doesn't return a per-stream or container-level `bit_rate` (the common case for live MPEG-TS / HLS):

```python
total_size = sum(int(p.get('size', 0)) for p in video_packets)
total_duration = sum(float(p.get('duration_time') or 0) for p in video_packets)
if total_duration > 0:
    video_bitrate = (total_size * 8) / (total_duration * 1000)
```

The math is correct, but it has no minimum-sample gate. With 2 packets — for example, one keyframe and an adjacent P-frame whose `duration_time` reports near zero — the size-to-duration ratio explodes. The 22924 kbps figure isn't a bug in the formula, it's a bug in trusting the formula at that sample size.

Healthy 4-second probes return 200–400 video packets. The 2-packet sample is rare and almost always indicates either a probe that timed out very early or a stream whose initial output was small fragments.

## What's fixed

New constant `PluginConfig.MIN_PACKETS_FOR_BITRATE_CALC = 30`. The packet-based fallback now only fires when `len(video_packets) >= 30` (≈1 second of 30 fps video). Below threshold:

- `video_bitrate` stays `None` for this probe
- `ffprobe_data.calculated_bitrate_kbps` is not written to `ffprobe_data` either
- The `dispatcharr_metadata.video_bitrate` field passed to `ChannelService.update_stream_stats` is `None`, so Dispatcharr's `stream_stats` keeps the prior value (or stays unset on a fresh stream)

The threshold sits well below the healthy floor (200+) and well above the pathological case (2), so legitimate measurements are not suppressed. If a slow-output stream consistently fails to clear 30 packets, the user can either raise `ffprobe_monitoring_seconds` to give it more time or drop the threshold by editing `PluginConfig`.

## How to verify

After deploy, scheduled runs whose probes captured fewer than 30 video packets will show `dispatcharr_metadata.video_bitrate=null` in `iptv_checker_results.json` (and an empty value in the CSV column) and `ffprobe_data` will lack the `calculated_bitrate_kbps` key. Healthy probes are unaffected. Since the next scheduled run window is the next morning, observed verification has to wait for tomorrow's resume window — expect to see the same 4-of-5 alive streams with intact bitrate values plus the previously-bogus stream either getting a real value (if its next probe captures enough packets) or staying at the old DB value (if the next probe also captures <30).
