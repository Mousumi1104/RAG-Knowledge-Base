"""DB-free unit tests for the EZ API-key gate and identity-in-prompt threading.

These deliberately avoid the DB/TestClient: they exercise the ``require_api_key``
dependency function directly (with the settings cache cleared between cases) and
the pure prompt-building helpers. See EZ chat auth spec §2/§3.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import employee_zone
from app.core.config import get_settings
from app.rag.prompt import _SYSTEM_PROMPT, _current_employee_line, build_rag_prompt


def _set_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Point EZ_CHAT_API_KEY at ``value`` and clear the cached Settings."""
    if value is None:
        monkeypatch.delenv("EZ_CHAT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("EZ_CHAT_API_KEY", value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    get_settings.cache_clear()


# --- §2 require_api_key -----------------------------------------------------

def test_gate_fail_open_when_key_unset(monkeypatch):
    _set_key(monkeypatch, None)
    # No header at all must be allowed while the key is unset (rollout window).
    assert employee_zone.require_api_key(x_api_key=None) is None


def test_gate_allows_matching_key(monkeypatch):
    _set_key(monkeypatch, "s3cret-key")
    assert employee_zone.require_api_key(x_api_key="s3cret-key") is None


def test_gate_rejects_wrong_key(monkeypatch):
    _set_key(monkeypatch, "s3cret-key")
    with pytest.raises(HTTPException) as exc:
        employee_zone.require_api_key(x_api_key="nope")
    assert exc.value.status_code == 401


def test_gate_rejects_missing_key_when_set(monkeypatch):
    _set_key(monkeypatch, "s3cret-key")
    with pytest.raises(HTTPException) as exc:
        employee_zone.require_api_key(x_api_key=None)
    assert exc.value.status_code == 401


# --- §3 identity-in-prompt --------------------------------------------------

def test_employee_line_blank_when_no_identity():
    assert _current_employee_line(None, None) == ""
    assert _current_employee_line("", "  ") == ""


def test_employee_line_with_name_and_id():
    line = _current_employee_line("Mousumi Sengupta", "123")
    assert "Current employee" in line
    assert "Mousumi Sengupta" in line
    assert "employee ID 123" in line
    assert "authoritative" in line


def test_employee_line_name_only():
    line = _current_employee_line("Mousumi", None)
    assert "assisting Mousumi" in line
    assert "employee ID" not in line


def test_build_rag_prompt_injects_employee():
    system, _user = build_rag_prompt("hi", [], employee_name="Mousumi", employee_id="123")
    assert "Current employee: you are assisting Mousumi" in system
    assert "employee ID 123" in system


def test_build_rag_prompt_back_compat_without_employee():
    # When no identity is passed, the system prompt must NOT gain the line.
    system, _user = build_rag_prompt("hi", [])
    assert "Current employee" not in system
    # Still starts from the static base + IST line (existing behaviour).
    assert system.startswith(_SYSTEM_PROMPT)
