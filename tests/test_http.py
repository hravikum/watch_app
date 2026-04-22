"""Tests for the HTTP client — cache behavior and rate limiting.

We mock httpx to avoid hitting the network.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from watch_app import http as http_module


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect the HTTP cache to a temp directory."""
    from watch_app.config import get_settings

    # Point the settings' data_dir at tmp_path so cache_dir lives under it.
    original = get_settings()

    class _PatchedSettings:
        data_dir = tmp_path
        cache_dir = tmp_path / "cache"
        # Pass through anything else accessed.
        db_url = original.db_url
        anthropic_api_key = original.anthropic_api_key
        llm_model = original.llm_model
        default_preset = original.default_preset

    monkeypatch.setattr(http_module, "get_settings", lambda: _PatchedSettings)
    # Also clear the rate-limit state between tests.
    http_module._last_request_at.clear()
    yield tmp_path


def _fake_response(status_code: int = 200, text: str = "hello world"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "text/plain"}
    resp.text = text
    return resp


def test_fresh_fetch_hits_network_and_caches(tmp_cache):
    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response(text="fresh data")
        response = http_module.get("https://example.com/a")
        assert response.text == "fresh data"
        assert mock_get.call_count == 1

    # Second call should hit the cache, not the network.
    with patch("watch_app.http.httpx.get") as mock_get:
        response2 = http_module.get("https://example.com/a")
        assert response2.text == "fresh data"
        assert mock_get.call_count == 0
        assert getattr(response2, "from_cache", False) is True


def test_use_cache_false_forces_refresh(tmp_cache):
    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response(text="v1")
        http_module.get("https://example.com/b")

    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response(text="v2")
        response = http_module.get("https://example.com/b", use_cache=False)
        assert response.text == "v2"
        assert mock_get.call_count == 1


def test_error_response_not_cached(tmp_cache):
    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response(status_code=500, text="err")
        http_module.get("https://example.com/error")

    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response(status_code=200, text="ok")
        response = http_module.get("https://example.com/error")
        # Should have refetched because the 500 wasn't cached.
        assert mock_get.call_count == 1
        assert response.text == "ok"


def test_rate_limit_throttles_same_host(tmp_cache, monkeypatch):
    # Reduce the interval so the test runs quickly but still measurable.
    monkeypatch.setattr(http_module, "MIN_REQUEST_INTERVAL", 0.2)

    with patch("watch_app.http.httpx.get") as mock_get:
        mock_get.return_value = _fake_response()
        t0 = time.monotonic()
        http_module.get("https://same-host.test/a", use_cache=False)
        http_module.get("https://same-host.test/b", use_cache=False)
        elapsed = time.monotonic() - t0

    assert elapsed >= 0.18  # allow small timing slack
