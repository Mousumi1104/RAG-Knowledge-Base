"""Offline unit tests for GeminiEmbeddingProvider rate-limit handling.

No network and no Gemini key required: ``httpx.post`` is stubbed and
``time.sleep`` / ``time.monotonic`` are monkeypatched so the tests run instantly
while still asserting the pacing/backoff behavior. These cover the fix for
large-document 429 failures (RPM pacing + stronger, Retry-After-aware backoff).
"""

from __future__ import annotations

import pytest

from app.rag import embeddings
from app.rag.embeddings import GeminiEmbeddingProvider
from app.utils.errors import RateLimitError


class _FakeResponse:
    """Minimal httpx.Response stand-in for stubbed ``httpx.post`` calls."""

    def __init__(self, status_code, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


def _ok_body(dim: int) -> dict:
    return {"embedding": {"values": [0.1] * dim}}


def _make_provider(**kwargs) -> GeminiEmbeddingProvider:
    defaults = dict(
        api_key="dummy-key",
        dim=4,
        rpm=10,
        max_retries=6,
        backoff_base_seconds=0.5,
        max_backoff_seconds=60.0,
    )
    defaults.update(kwargs)
    return GeminiEmbeddingProvider(**defaults)


def _patch_clock(monkeypatch):
    """Replace time.sleep/time.monotonic with controllable fakes.

    sleep() records the requested delays AND advances a virtual clock so the next
    monotonic() read reflects elapsed time. Returns the list of recorded sleeps.
    """
    state = {"now": 1000.0}
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        state["now"] += seconds

    def fake_monotonic():
        return state["now"]

    monkeypatch.setattr(embeddings.time, "sleep", fake_sleep)
    monkeypatch.setattr(embeddings.time, "monotonic", fake_monotonic)
    return sleeps, state


def test_pacing_enforces_rpm_spacing(monkeypatch):
    """Sequential embeds sleep ~60/RPM between requests (first call not delayed)."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider(rpm=10)  # min interval = 6.0s

    posts: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(url)
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    result = provider.embed_texts(["a", "b", "c"])

    assert len(result) == 3
    assert len(posts) == 3
    # First call must NOT be delayed; the two subsequent calls each wait one
    # full interval (60/10 = 6.0s). The pacing sleep for the first call is 0 or
    # absent, so non-zero pacing sleeps should equal 6.0 for the gaps.
    pacing_sleeps = [s for s in sleeps if s > 0]
    assert len(pacing_sleeps) == 2
    assert all(abs(s - 6.0) < 1e-6 for s in pacing_sleeps)


def test_429_then_200_retries_and_returns(monkeypatch):
    """A single 429 is retried with backoff, then the 200 embedding is returned."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider()

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, {}, text="rate limited")
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    vec = provider.embed_query("hello")

    assert calls["n"] == 2
    assert len(vec) == provider.dim
    # At least one backoff sleep (the 0.5 * 2**0 = 0.5s for attempt 0) occurred.
    assert any(abs(s - 0.5) < 1e-6 for s in sleeps)


def test_persistent_429_raises_no_fallback(monkeypatch):
    """Always-429 exhausts retries and raises; never returns a hash/fallback vector."""
    _patch_clock(monkeypatch)
    provider = _make_provider(max_retries=3)

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(429, {}, text="rate limited")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    with pytest.raises(RateLimitError):
        provider.embed_query("hello")

    # max_retries=3 → attempts 0..3 inclusive = 4 total POSTs.
    assert calls["n"] == 4


def test_503_then_200_retries_and_returns(monkeypatch):
    """A transient 503 (Gemini overloaded) is retried with backoff, then succeeds."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider()

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(503, {}, text="service unavailable")
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    vec = provider.embed_query("hello")

    assert calls["n"] == 2
    assert len(vec) == provider.dim
    # The first attempt's backoff sleep (0.5 * 2**0 = 0.5s) occurred.
    assert any(abs(s - 0.5) < 1e-6 for s in sleeps)


def test_non_transient_4xx_raises_immediately(monkeypatch):
    """A 400/403 (non-transient) is NOT retried — it raises on the first response."""
    _patch_clock(monkeypatch)
    provider = _make_provider()

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(400, {}, text="bad request")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    from app.utils.errors import ProviderError

    with pytest.raises(ProviderError):
        provider.embed_query("hello")

    assert calls["n"] == 1  # no retries for non-transient 4xx


def test_retry_after_is_honored_and_capped(monkeypatch):
    """A Retry-After larger than computed backoff is honored, capped at the ceiling."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider(max_backoff_seconds=60.0)

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # Header asks for 45s — far above the 0.5s base backoff.
            return _FakeResponse(429, {}, headers={"Retry-After": "45"}, text="429")
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    vec = provider.embed_query("hi")

    assert len(vec) == provider.dim
    # The backoff sleep must be >= 45 (Retry-After honored) and <= 60 (ceiling).
    backoff_sleeps = [s for s in sleeps if s >= 45.0]
    assert backoff_sleeps, f"expected a >=45s backoff sleep, got {sleeps}"
    assert all(s <= 60.0 for s in backoff_sleeps)


def test_retry_after_from_json_body_retrydelay(monkeypatch):
    """retryDelay in the JSON error body (e.g. '30s') is parsed and honored."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider(max_backoff_seconds=60.0)

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            body = {
                "error": {
                    "code": 429,
                    "details": [{"retryDelay": "30s"}],
                }
            }
            return _FakeResponse(429, body, text="429")
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    vec = provider.embed_query("hi")

    assert len(vec) == provider.dim
    assert any(s >= 30.0 for s in sleeps), f"expected a >=30s backoff sleep, got {sleeps}"


def test_first_call_not_delayed(monkeypatch):
    """The very first embed request incurs no pacing sleep."""
    sleeps, _ = _patch_clock(monkeypatch)
    provider = _make_provider()

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(200, _ok_body(provider.dim))

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    provider.embed_query("first")

    assert all(s == 0 for s in sleeps), f"first call should not sleep, got {sleeps}"
