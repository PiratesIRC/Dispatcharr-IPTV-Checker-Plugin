"""The routing simulation must agree with Newsflasharr's real evaluator.

iptv_checker/notify_report.py contains a COPY of newsflasharr/routing.py's
`route` and `_rule_matches`, because a Dispatcharr plugin cannot import another
plugin: each is deployed as its own directory under /data/plugins/. The copy is
what lets the preflight predict where a report would actually land instead of
merely confirming that some rule exists.

A copy drifts, and the symptom of drift is the worst available: a preflight that
reports clean while mail does not arrive. This compares the two implementations
over a matrix of rule shapes rather than trusting hand-written expectations.

Newsflasharr lives outside this repository, so this file SKIPS where it is
absent, such as a repository-only CI checkout. Same tradeoff as the vendored
client parity gate.
"""
import importlib.util
import itertools
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MINE_PATH = _ROOT / "iptv_checker" / "notify_report.py"
_THEIRS_PATH = _ROOT.parent / "notifier" / "newsflasharr" / "routing.py"

_spec = importlib.util.spec_from_file_location("ic_notify_report_parity", _MINE_PATH)
mine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mine)


def _load_theirs():
    spec = importlib.util.spec_from_file_location("nf_routing", _THEIRS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pytestmark = pytest.mark.skipif(
    not _THEIRS_PATH.exists(),
    reason="Newsflasharr is not present in this checkout, so the real routing "
           "evaluator cannot be compared against",
)

# Rule shapes chosen to cover the cases that actually decide the outcome:
# a targeted match, a source-only catch-all, a rule with NO match key (which
# matches every event because all([]) is True), exclusive and additive forms,
# and a rule whose channels key is missing entirely.
_RULES = [
    {"match": {"source": "iptv_checker", "event": "usage_report"},
     "channels": ["smtp"], "exclusive": True},
    {"match": {"source": "iptv_checker", "event": "usage_report"},
     "channels": ["smtp"]},
    {"match": {"source": "iptv_checker"}, "channels": ["ticker"], "exclusive": True},
    {"match": {"source": "iptv_checker"}, "channels": ["ticker"]},
    {"match": {"source": "sentinelarr"}, "channels": ["smtp"], "exclusive": True},
    {"match": {"event": "usage_report"}, "channels": ["push", "smtp"]},
    {"channels": ["ticker"], "exclusive": True},
    {"match": {}, "channels": ["push"]},
    {"match": {"source": "iptv_checker", "event": "usage_report"}},
]

_EVENTS = [
    {"source": "iptv_checker", "event": "usage_report"},
    {"source": "iptv_checker", "event": "other"},
    {"source": "sentinelarr", "event": "usage_report"},
    {"source": "", "event": ""},
]

_DEFAULTS = [[], ["ticker"], ["ticker", "push"], ["smtp"]]


def test_single_rule_parity():
    theirs = _load_theirs()
    for rule, event, defaults in itertools.product(_RULES, _EVENTS, _DEFAULTS):
        assert mine.route(event, [rule], defaults) == theirs.route(event, [rule], defaults), (
            "routing disagreed for rule=%r event=%r defaults=%r" % (rule, event, defaults))


def test_rule_pair_parity_including_order():
    """Order matters: an earlier rule contributes its channels first, and
    exclusivity is decided across ALL matching rules rather than the first."""
    theirs = _load_theirs()
    for first, second in itertools.permutations(_RULES, 2):
        for event in _EVENTS:
            for defaults in ([], ["ticker"]):
                pair = [first, second]
                assert mine.route(event, pair, defaults) == theirs.route(event, pair, defaults), (
                    "routing disagreed for rules=%r event=%r defaults=%r"
                    % (pair, event, defaults))


def test_empty_rule_set_parity():
    theirs = _load_theirs()
    for event in _EVENTS:
        for defaults in _DEFAULTS:
            assert mine.route(event, [], defaults) == theirs.route(event, [], defaults)


def test_the_match_everything_case_is_reproduced():
    """A rule with no match key matches EVERY event, because all([]) is True.
    Newsflasharr's own linter warns about it, so the copy must reproduce it."""
    theirs = _load_theirs()
    no_match = {"channels": ["ticker"], "exclusive": True}
    for event in _EVENTS:
        assert mine.route(event, [no_match], ["push"]) == theirs.route(event, [no_match], ["push"])
        assert "ticker" in mine.route(event, [no_match], ["push"])
