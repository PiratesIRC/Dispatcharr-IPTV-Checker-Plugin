"""RateLimitGuard behavior + HTTP 429 classification in check_stream.

Covers the v1.26.1181025 incident class: 429s misclassified (or missed) and
the sliding-window/cooldown logic that protects providers from probe bursts.
"""
class _FakeCompleted:
    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _probe(plugin, pmod, monkeypatch, quiet_logger, stderr):
    """Run check_stream against a mocked ffprobe that fails with `stderr`."""
    monkeypatch.setattr(
        pmod.subprocess, "run", lambda *a, **k: _FakeCompleted(stderr=stderr)
    )
    stream = {"stream_url": "http://example.com/live/1.ts", "channel_name": "Test", "stream_id": 1}
    settings = {"probe_timeout": 1, "ffprobe_analysis_duration": 1}
    return plugin.check_stream(stream, 1, 0, quiet_logger, skip_retries=True, settings=settings)


# --- 429 classification -----------------------------------------------------

def test_http_429_classified_as_skipped_rate_limited(plugin, pmod, monkeypatch, quiet_logger):
    result = _probe(plugin, pmod, monkeypatch, quiet_logger,
                    "Server returned HTTP error 429 Too Many Requests")
    assert result["status"] == "Skipped"
    assert result["error_type"] == "Rate Limited"


def test_too_many_requests_phrase_classified(plugin, pmod, monkeypatch, quiet_logger):
    result = _probe(plugin, pmod, monkeypatch, quiet_logger,
                    "upstream said: too many requests, slow down")
    assert result["status"] == "Skipped"
    assert result["error_type"] == "Rate Limited"


def test_429_substring_in_stream_id_is_not_rate_limited(plugin, pmod, monkeypatch, quiet_logger):
    """The \\b429\\b word boundary: '14290' must NOT trip rate-limit handling."""
    result = _probe(plugin, pmod, monkeypatch, quiet_logger,
                    "error opening stream id 14290: connection refused")
    assert result["status"] == "Dead"
    assert result["error_type"] != "Rate Limited"


def test_rate_limited_streams_record_a_guard_hit(plugin, pmod, monkeypatch, quiet_logger):
    _probe(plugin, pmod, monkeypatch, quiet_logger, "HTTP error 429")
    assert len(plugin._rate_limit_guard._hit_times) == 1


# --- sliding window / cooldown ----------------------------------------------

def test_guard_does_not_trip_below_threshold(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD - 1):
        guard.record_hit(quiet_logger)
    assert guard._cooldown_until == 0.0


def test_guard_trips_at_threshold_within_window(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD):
        guard.record_hit(quiet_logger)
        fake_clock.advance(1)
    assert guard._cooldown_until > fake_clock.time()
    assert guard._cooldown_until - fake_clock.time() <= guard.BASE_COOLDOWN_SECONDS


def test_guard_does_not_trip_when_hits_spread_past_window(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD):
        guard.record_hit(quiet_logger)
        fake_clock.advance(guard.WINDOW_SECONDS + 1)  # each hit ages out
    assert guard._cooldown_until == 0.0


def test_cooldown_doubles_on_retrip_and_caps(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()

    def trip():
        for _ in range(guard.TRIP_THRESHOLD):
            guard.record_hit(quiet_logger)

    trip()
    first = guard._cooldown_until - fake_clock.time()
    assert first == guard.BASE_COOLDOWN_SECONDS

    # Let the first cooldown expire, then trip again quickly (no decay window).
    fake_clock.advance(first + 1)
    trip()
    second = guard._cooldown_until - fake_clock.time()
    assert second == guard.BASE_COOLDOWN_SECONDS * 2

    # Keep re-tripping: cooldown must never exceed the cap.
    for _ in range(8):
        fake_clock.advance(guard._cooldown_until - fake_clock.time() + 1)
        trip()
    assert guard._cooldown_until - fake_clock.time() <= guard.MAX_COOLDOWN_SECONDS


def test_cooldown_growth_decays_after_clean_stretch(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD):
        guard.record_hit(quiet_logger)
    assert guard._next_cooldown == guard.BASE_COOLDOWN_SECONDS * 2

    # A clean stretch longer than DECAY_AFTER_SECONDS resets the doubling.
    fake_clock.advance(guard.DECAY_AFTER_SECONDS + guard.BASE_COOLDOWN_SECONDS + 1)
    guard.wait_if_throttled(quiet_logger)
    assert guard._next_cooldown == guard.BASE_COOLDOWN_SECONDS


def test_wait_if_throttled_returns_immediately_when_clean(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    start = fake_clock.time()
    guard.wait_if_throttled(quiet_logger)
    assert fake_clock.time() == start


def test_wait_if_throttled_sleeps_until_cooldown_end(pmod, fake_clock, quiet_logger):
    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD):
        guard.record_hit(quiet_logger)
    deadline = guard._cooldown_until
    guard.wait_if_throttled(quiet_logger)
    assert fake_clock.time() >= deadline


def test_wait_if_throttled_honors_stop_event(pmod, fake_clock, quiet_logger):
    import threading

    guard = pmod.RateLimitGuard()
    for _ in range(guard.TRIP_THRESHOLD):
        guard.record_hit(quiet_logger)
    stop = threading.Event()
    stop.set()
    start = fake_clock.time()
    guard.wait_if_throttled(quiet_logger, stop_event=stop)
    # Bails on the stop event after at most one 1s sleep tick.
    assert fake_clock.time() - start <= 1.0
