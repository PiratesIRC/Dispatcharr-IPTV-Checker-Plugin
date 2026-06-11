"""Webhook payload shaping and delivery headers.

Covers issue #20: Discord's Cloudflare edge 403s the default Python-urllib
User-Agent (silently dropping every webhook), and Discord only renders
{"content"/"embeds"} payloads — arbitrary JSON keys are ignored.
"""
import json
import urllib.error


RESULTS = [
    {"status": "Alive"}, {"status": "Alive"}, {"status": "Alive"},
    {"status": "Dead"},
    {"status": "Skipped"}, {"status": "Skipped"},
]


def _write_results(plugin):
    with open(plugin.results_file, "w") as f:
        json.dump(RESULTS, f)


class _FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_request(pmod, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["request"] = req
        return _FakeResponse()

    monkeypatch.setattr(pmod.urllib.request, "urlopen", fake_urlopen)
    return captured


# --- guard clauses -----------------------------------------------------------

def test_no_url_configured_is_error(plugin, quiet_logger):
    result = plugin._fire_webhook({"webhook_url": ""}, quiet_logger)
    assert result["status"] == "error"


def test_non_http_url_is_error(plugin, quiet_logger):
    result = plugin._fire_webhook({"webhook_url": "ftp://example.com/hook"}, quiet_logger)
    assert result["status"] == "error"


def test_no_results_file_is_graceful(plugin, quiet_logger):
    result = plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, quiet_logger)
    assert result["status"] == "ok"
    assert "No results" in result["message"]


# --- Discord-specific shaping --------------------------------------------------

def test_discord_url_gets_native_content_payload(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)
    captured = _capture_request(pmod, monkeypatch)
    result = plugin._fire_webhook(
        {"webhook_url": "https://discord.com/api/webhooks/123/abc"}, quiet_logger
    )
    assert result["status"] == "ok"
    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert set(payload.keys()) == {"content"}, "Discord renders only content/embeds keys"
    assert "Alive: 3" in payload["content"]
    assert "Dead: 1" in payload["content"]
    assert "Skipped: 2" in payload["content"]


def test_discordapp_and_subdomain_hosts_detected(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)
    for url in (
        "https://discordapp.com/api/webhooks/123/abc",
        "https://ptb.discord.com/api/webhooks/123/abc",
    ):
        captured = _capture_request(pmod, monkeypatch)
        plugin._fire_webhook({"webhook_url": url}, quiet_logger)
        payload = json.loads(captured["request"].data.decode("utf-8"))
        assert "content" in payload, f"{url} not detected as Discord"


def test_lookalike_host_is_not_discord(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)
    captured = _capture_request(pmod, monkeypatch)
    plugin._fire_webhook({"webhook_url": "https://notdiscord.com/hook"}, quiet_logger)
    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert "content" not in payload


# --- generic payload (backward compatibility) -----------------------------------

def test_generic_payload_shape_and_counts(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)
    captured = _capture_request(pmod, monkeypatch)
    result = plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, quiet_logger)
    assert result["status"] == "ok"
    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert payload["plugin"] == "iptv_checker"
    assert payload["event"] == "check_complete"
    assert payload["total"] == 6
    assert payload["alive"] == 3
    assert payload["dead"] == 1
    assert payload["skipped"] == 2
    assert "timestamp" in payload


# --- headers ------------------------------------------------------------------

def test_explicit_user_agent_always_set(plugin, pmod, monkeypatch, quiet_logger):
    """The default Python-urllib/3.x UA is 403'd by Discord's Cloudflare edge."""
    _write_results(plugin)
    captured = _capture_request(pmod, monkeypatch)
    plugin._fire_webhook({"webhook_url": "https://discord.com/api/webhooks/123/abc"}, quiet_logger)
    ua = captured["request"].get_header("User-agent", "")
    assert ua.startswith("Dispatcharr-IPTV-Checker/")
    assert "Python-urllib" not in ua


def test_content_type_is_json(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)
    captured = _capture_request(pmod, monkeypatch)
    plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, quiet_logger)
    assert captured["request"].get_header("Content-type") == "application/json"


# --- error handling --------------------------------------------------------------

def test_http_error_reported_not_raised(plugin, pmod, monkeypatch, quiet_logger):
    _write_results(plugin)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", None, None)

    monkeypatch.setattr(pmod.urllib.request, "urlopen", fake_urlopen)
    result = plugin._fire_webhook({"webhook_url": "https://example.com/hook"}, quiet_logger)
    assert result["status"] == "error"
    assert "403" in result["message"]
