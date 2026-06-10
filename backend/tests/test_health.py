"""Smoke tests for the application foundation."""

from __future__ import annotations


def test_health_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
