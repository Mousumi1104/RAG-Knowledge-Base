"""Offline unit tests for the Gemini LLM provider helpers and factory wiring.

No network: these cover the pure parsing/extraction functions, the request
payload shape, and provider selection (including the no-key local fallback).
"""

from __future__ import annotations

from app.rag import llm


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
    monkeypatch.setattr(llm.settings, "GEMINI_MODEL", "gemini-2.5-flash")
    provider = llm.get_llm_provider()
    assert isinstance(provider, llm.GeminiLLMProvider)
    assert provider._model == "gemini-2.5-flash"
