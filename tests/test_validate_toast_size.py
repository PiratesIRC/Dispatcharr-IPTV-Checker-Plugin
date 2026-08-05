"""The Validate Settings result must fit the toast Dispatcharr actually renders.

Dispatcharr's plugin card renders a transient toast of roughly 280 characters,
clipped from the MIDDLE with no ellipsis, and newlines collapse into one
paragraph. A result longer than that does not scroll: its middle is silently
removed, so the operator sees the beginning and the end of a sentence with the
finding cut out from between them.

MEASURED ON LIVE DATA 2026-08-05: enumerating group names produced a single
line of 4,690 characters against a 952-group install, 16.8 times the entire
budget. Counts convey the same information in about 50.

So the rule these tests bind is: report a COUNT, never a list of group names.
The count is what the operator needs in order to spot that a filter selected
far more or far fewer groups than intended.
"""
import re

# Deliberately below Dispatcharr's ~280 character clip point, so there is room
# for the version line and the trailing "Settings valid" sentence.
TOAST_BUDGET = 280


class _Objs:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


def _stub_orm(pmod, monkeypatch, channels=1440, groups=952, streams=20000):
    monkeypatch.setattr(pmod.Channel, "objects", _Objs(channels), raising=False)
    monkeypatch.setattr(pmod.ChannelGroup, "objects", _Objs(groups), raising=False)
    monkeypatch.setattr(pmod.Stream, "objects", _Objs(streams), raising=False)


def _many_groups(n):
    """Group names shaped like the real ones on the live box."""
    out = [{"id": i, "name": f"US- Provider Category Name {i:04d}"} for i in range(n)]
    out += [{"id": 10000 + i, "name": f"US- PPV Event Channel {i:04d}"} for i in range(257)]
    return out


def _run(plugin, pmod, monkeypatch, quiet_logger, settings, n=695):
    _stub_orm(pmod, monkeypatch)
    monkeypatch.setattr(plugin, "_get_all_groups", lambda logger: _many_groups(n))
    # _get_latest_version calls GitHub over the network; stub it.
    monkeypatch.setattr(plugin, "_get_latest_version",
                        lambda *a, **k: ("1.26.2171124", "Up to date"))
    return plugin.validate_settings_action(settings, quiet_logger)


def test_exclude_mode_result_fits_the_toast(plugin, pmod, monkeypatch, quiet_logger):
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "*PPV*", "channel_groups_mode": "exclude"})
    body = res.get("message") or res.get("error") or ""
    assert len(body) <= TOAST_BUDGET, (
        f"Validate Settings returned {len(body)} characters against a ~{TOAST_BUDGET} "
        f"character toast; the middle would be silently clipped. Body: {body[:200]!r}"
    )


def test_include_mode_result_fits_the_toast(plugin, pmod, monkeypatch, quiet_logger):
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "US-*", "channel_groups_mode": "include"})
    body = res.get("message") or res.get("error") or ""
    assert len(body) <= TOAST_BUDGET, f"{len(body)} characters"


def test_no_groups_specified_fits_the_toast(plugin, pmod, monkeypatch, quiet_logger):
    res = _run(plugin, pmod, monkeypatch, quiet_logger, {})
    body = res.get("message") or res.get("error") or ""
    assert len(body) <= TOAST_BUDGET, f"{len(body)} characters"


def test_group_names_are_never_enumerated(plugin, pmod, monkeypatch, quiet_logger):
    """The specific regression. One real group name appearing in the result
    means the list is being printed, and the list scales with the install."""
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "*PPV*", "channel_groups_mode": "exclude"})
    body = res.get("message") or res.get("error") or ""
    assert "US- PPV Event Channel 0000" not in body
    assert "US- Provider Category Name 0000" not in body


def test_the_count_is_actually_reported(plugin, pmod, monkeypatch, quiet_logger):
    """Bounding the length is only half the requirement: the number of groups
    that WILL BE CHECKED has to survive the trimming, because that is the
    number the operator is validating."""
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "*PPV*", "channel_groups_mode": "exclude"}, n=695)
    body = res.get("message") or res.get("error") or ""
    assert "695" in body, f"the checked-group count is missing from: {body!r}"


def test_skipped_count_reported_in_exclude_mode(plugin, pmod, monkeypatch, quiet_logger):
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "*PPV*", "channel_groups_mode": "exclude"}, n=695)
    body = res.get("message") or res.get("error") or ""
    assert "257" in body, f"the skipped-group count is missing from: {body!r}"


def test_a_typo_still_reports_as_an_error(plugin, pmod, monkeypatch, quiet_logger):
    """Trimming the output must not trim away the finding. A pattern matching
    nothing is an error in both modes: in exclude mode a typo leaves the group
    being checked and acted on."""
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "Nope-*", "channel_groups_mode": "exclude"})
    assert res["status"] == "error"
    body = res.get("message") or res.get("error") or ""
    assert "Nope-*" in body, "the unmatched pattern must be named so it can be fixed"
    assert len(body) <= TOAST_BUDGET, f"{len(body)} characters"


def test_many_unmatched_patterns_do_not_blow_the_budget(plugin, pmod, monkeypatch, quiet_logger):
    """The unmatched list is operator-typed, so it is bounded in practice, but
    it must not be the next thing that overflows."""
    typos = ", ".join(f"NoSuchGroup{i:03d}-*" for i in range(60))
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": typos, "channel_groups_mode": "include"})
    body = res.get("message") or res.get("error") or ""
    assert len(body) <= TOAST_BUDGET, f"{len(body)} characters"


def test_result_sets_exactly_one_of_message_or_error(plugin, pmod, monkeypatch, quiet_logger):
    """Dispatcharr renders `error` in red and persistently, and `message` as a
    transient green toast. A failure that sets neither is pixel-identical to
    success."""
    ok = _run(plugin, pmod, monkeypatch, quiet_logger,
              {"channel_groups": "US-*", "channel_groups_mode": "include"})
    bad = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "Nope-*", "channel_groups_mode": "include"})
    for res in (ok, bad):
        assert ("message" in res) != ("error" in res), (
            f"set exactly one of message/error, got keys {sorted(res)}"
        )


def test_no_control_characters_survive(plugin, pmod, monkeypatch, quiet_logger):
    """Newlines collapse into one paragraph in the toast, so a result built for
    a multi-line readout reads as run-together prose. Keep separators explicit."""
    res = _run(plugin, pmod, monkeypatch, quiet_logger,
               {"channel_groups": "US-*", "channel_groups_mode": "include"})
    body = res.get("message") or res.get("error") or ""
    assert not re.search(r"\n\s*\n", body), "blank lines collapse to nothing in the toast"
