"""Unit tests for :mod:`app.rag.prompt`.

Covers two behaviours added to the RAG system prompt:

- An authoritative real-time clock line in India Standard Time (IST), injected
  into the *returned* system prompt by :func:`build_rag_prompt` while the static
  ``_SYSTEM_PROMPT`` base is left unchanged.
- A human-voice tone guard: no robotic "Based on the document" prefaces and no
  AI self-references.

The clock helper takes an injectable ``now`` so these tests stay deterministic
and never depend on the wall clock.
"""

from __future__ import annotations

from datetime import datetime

from app.rag.prompt import (
    IST,
    _SYSTEM_PROMPT,
    _current_ist_line,
    build_rag_prompt,
)


def test_current_ist_line_renders_expected_date() -> None:
    line = _current_ist_line(datetime(2026, 6, 30, 11, 5, tzinfo=IST))
    assert "IST" in line
    assert "30 June 2026" in line
    # 2026-06-30 is a Tuesday (weekday derived by strftime, not hard-coded).
    assert "Tuesday" in line
    assert "11:05 AM" in line
    # No leading zero on the day (portable, not via %-d).
    assert "30 June" in line and " 030 " not in line


def test_current_ist_line_strips_leading_zero_on_single_digit_day_and_hour() -> None:
    line = _current_ist_line(datetime(2026, 1, 5, 9, 7, tzinfo=IST))
    # Day "5" and hour "9" must have no leading zeros.
    assert "5 January 2026" in line
    assert "9:07 AM IST" in line


def test_build_rag_prompt_injects_ist_clock_into_system() -> None:
    system, _user = build_rag_prompt("hi", [])
    assert "IST" in system
    assert "India" in system
    assert "UTC+05:30" in system
    # The current-date wording is present and marked authoritative/exempt.
    assert "Current date and time" in system
    assert "authoritative" in system
    assert "exempt from the context-only grounding rule" in system


def test_static_system_prompt_is_not_mutated() -> None:
    # The dynamic line is only on the RETURNED string; the base stays static.
    assert "Current date and time" not in _SYSTEM_PROMPT
    system, _user = build_rag_prompt("hi", [])
    assert system.startswith(_SYSTEM_PROMPT)


def test_grounding_rule_still_present() -> None:
    assert "ONLY the information in the provided context" in _SYSTEM_PROMPT


def test_human_tone_guidance_present() -> None:
    # Requirement B1: no "Based on the document" style prefaces.
    assert "Based on the document" in _SYSTEM_PROMPT
    assert "NEVER begin a reply with robotic meta-prefaces" in _SYSTEM_PROMPT
    # Requirement B2: never self-identify as an AI/model/bot.
    assert "Never refer to yourself as an AI" in _SYSTEM_PROMPT
    assert "human expert" in _SYSTEM_PROMPT
