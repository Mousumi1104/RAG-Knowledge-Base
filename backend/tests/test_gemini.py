"""Offline unit tests for the Gemini LLM provider helpers and factory wiring.

No network: these cover the pure parsing/extraction functions, the request
payload shape, and provider selection (including the no-key local fallback).
"""

from __future__ import annotations

import pytest

from app.rag import llm
from app.utils.errors import RateLimitError


class _FakeResponse:
    """Minimal httpx.Response stand-in for stubbed ``httpx.post`` calls."""

    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


def test_gemini_extract_text_concatenates_parts_without_stripping():
    data = {
        "candidates": [
            {"content": {"parts": [{"text": "Female"}, {"text": " employees"}]}}
        ]
    }
    # Must NOT strip — streaming relies on boundary whitespace being preserved.
    assert llm._gemini_extract_text(data) == "Female employees"


def test_gemini_extract_text_handles_missing_or_empty_shapes():
    assert llm._gemini_extract_text({}) == ""
    assert llm._gemini_extract_text({"candidates": []}) == ""
    # A thinking-only response (no parts) yields empty text rather than raising.
    assert llm._gemini_extract_text({"candidates": [{"content": {"role": "model"}}]}) == ""
    assert llm._gemini_extract_text("not-a-dict") == ""


def test_parse_gemini_sse_line():
    line = 'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}'
    assert llm._parse_gemini_sse_line(line) == "hi"
    # Non-data / malformed lines are ignored (empty string).
    assert llm._parse_gemini_sse_line("event: ping") == ""
    assert llm._parse_gemini_sse_line("data: {not json}") == ""


def test_gemini_payload_disables_thinking_and_sets_system():
    provider = llm.GeminiLLMProvider(api_key="dummy", model="gemini-2.5-flash")
    payload = provider._payload("the question", system="be brief")
    assert payload["contents"][0]["parts"][0]["text"] == "the question"
    assert payload["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    assert payload["systemInstruction"]["parts"][0]["text"] == "be brief"
    # No system → no systemInstruction key.
    assert "systemInstruction" not in provider._payload("q", system=None)


def test_gemini_stream_url_uses_sse():
    provider = llm.GeminiLLMProvider(api_key="dummy", model="gemini-2.5-flash")
    assert provider._url("streamGenerateContent", sse=True).endswith(
        "gemini-2.5-flash:streamGenerateContent?alt=sse"
    )
    assert provider._url("generateContent").endswith("gemini-2.5-flash:generateContent")


def test_get_llm_provider_falls_back_to_local_without_key(monkeypatch):
    monkeypatch.setattr(llm.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", None)
    assert isinstance(llm.get_llm_provider(), llm.LocalExtractiveLLMProvider)


def test_get_llm_provider_selects_gemini_with_key(monkeypatch):
    monkeypatch.setattr(llm.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY_FALLBACK", None)
    monkeypatch.setattr(llm.settings, "GEMINI_MODEL", "gemini-2.5-flash")
    provider = llm.get_llm_provider()
    assert isinstance(provider, llm.GeminiLLMProvider)
    assert provider._model == "gemini-2.5-flash"


def test_get_llm_provider_passes_fallback_key(monkeypatch):
    monkeypatch.setattr(llm.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", "primary")
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY_FALLBACK", "fallback")
    monkeypatch.setattr(llm.settings, "GEMINI_MODEL", "gemini-2.5-flash")
    provider = llm.get_llm_provider()
    assert isinstance(provider, llm.GeminiLLMProvider)
    assert provider._keys == ["primary", "fallback"]


def test_gemini_generate_rotates_to_fallback_on_429(monkeypatch):
    """A 429 on the PRIMARY key rotates to the FALLBACK key instantly (no sleep)."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    provider = llm.GeminiLLMProvider(api_keys=["primary", "fallback"])

    used_keys: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        key = (headers or {}).get("x-goog-api-key")
        used_keys.append(key)
        if key == "primary":
            return _FakeResponse(429, {}, text="rate limited")
        return _FakeResponse(
            200, {"candidates": [{"content": {"parts": [{"text": "hi there"}]}}]}
        )

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    answer = provider.generate("the question")

    assert answer == "hi there"
    # Primary tried first (429), then fallback (200) on the same pass.
    assert used_keys == ["primary", "fallback"]
    # Instant 429->rotate incurs no backoff sleep.
    assert sleeps == []


def test_gemini_generate_all_keys_exhausted_raises(monkeypatch):
    """When every key 429s across all passes, RateLimitError is raised."""
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    provider = llm.GeminiLLMProvider(api_keys=["primary", "fallback"])

    used_keys: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        used_keys.append((headers or {}).get("x-goog-api-key"))
        return _FakeResponse(429, {}, text="rate limited")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    with pytest.raises(RateLimitError):
        provider.generate("the question")

    # 2 keys * (_MAX_RETRIES + 1 passes) POSTs, all 429.
    assert set(used_keys) == {"primary", "fallback"}
    assert len(used_keys) == 2 * (llm._MAX_RETRIES + 1)


def test_gemini_generate_back_compat_single_key(monkeypatch):
    """Single api_key construction still generates on a 200."""
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    provider = llm.GeminiLLMProvider(api_key="solo")

    used_keys: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        used_keys.append((headers or {}).get("x-goog-api-key"))
        return _FakeResponse(
            200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)

    assert provider.generate("q") == "ok"
    assert used_keys == ["solo"]
