"""Shared pytest fixtures and global test isolation.

Tests run against a *real* local PostgreSQL + pgvector instance (NOT SQLite and
NOT ChromaDB):

- DB:    postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test
- vector store: pgvector (embeddings live on the ``chunks.embedding`` column)
- providers: local embedding + local LLM (deterministic, no network, no API keys)

CRITICAL ORDERING: environment variables that drive configuration must be set
*before* any ``app`` module is imported, because:

- ``app.core.config`` instantiates a module-level ``settings`` object at import
  time (and ``get_settings`` is ``lru_cache``-d).
- ``app.database.session`` builds the SQLAlchemy ``engine`` / ``SessionLocal``
  at import time from ``settings.DATABASE_URL``.

So we set env -> clear the settings cache -> only then import app modules. This
guarantees the Postgres test DB and the pgvector store are the ones actually
used.

Schema setup: a session-scoped fixture runs the real Alembic migration
(``alembic upgrade head``) against ``ragdb_test`` so the test schema is
*identical* to production (vector extension + tables + HNSW ANN index).
Per-test isolation truncates the data tables (``chunks``, ``documents``) with
``RESTART IDENTITY CASCADE``; because embeddings are co-located on ``chunks``,
this clears the vectors too in one shot. The HNSW index persists (correct).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1) Set environment BEFORE importing any app module.
# ---------------------------------------------------------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="rag_tests_"))
_UPLOADS_ROOT = _TMP_ROOT / "uploads"
_UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)

_TEST_DATABASE_URL = "postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test"

os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"
os.environ["VECTOR_DB"] = "pgvector"
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
# No API keys: local fallback providers, no network.
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

# ---------------------------------------------------------------------------
# 2) Clear the cached settings so our env is picked up, then import app modules.
# ---------------------------------------------------------------------------
from app.core import config as _config  # noqa: E402

_config.get_settings.cache_clear()
# Rebind the module-level convenience instance to a fresh settings object.
_config.settings = _config.get_settings()

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database import session as db_session  # noqa: E402
from app.services import (  # noqa: E402
    document_service,  # noqa: E402
    indexing_service,
    search_service,
)

# Redirect document uploads into a temp dir so we never pollute backend/uploads.
document_service.UPLOADS_DIR = _UPLOADS_ROOT

# Backend root (contains alembic.ini / migrations/) for running the migration.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _reset_service_caches() -> None:
    """Drop the module-level cached provider/store singletons in both services."""
    indexing_service.reset_caches()
    search_service.reset_caches()


# ---------------------------------------------------------------------------
# 3) Pre-flight + schema setup (session scoped).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _database_schema():
    """Verify Postgres is reachable, then build the schema via Alembic.

    Fails fast with an actionable message if the local Postgres / ``ragdb_test``
    is unreachable, then runs ``alembic upgrade head`` so the test DB has the
    exact migrated schema (vector extension + tables + HNSW index).
    """
    # Pre-flight: confirm the test DB is reachable.
    try:
        with db_session.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:  # pragma: no cover - environment guard
        pytest.exit(
            "Cannot reach the test PostgreSQL at "
            f"{settings.DATABASE_URL!r}: {exc}\n"
            "Start the local userspace Postgres first, e.g.:\n"
            "    bash backend/scripts/pg_local.sh up",
            returncode=1,
        )

    # Build the schema with the real migration (idempotent across runs because
    # we downgrade to base first to guarantee a clean, current schema).
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": _TEST_DATABASE_URL}
    # Use the same interpreter running pytest so Alembic resolves the venv deps.
    python = sys.executable or "python"

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [python, "-m", "alembic", *args],
            cwd=str(_BACKEND_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )

    # Reset to a known-clean schema, then upgrade. ``downgrade base`` is a no-op
    # the first time (nothing to drop) and clears any leftovers on reruns.
    _alembic("downgrade", "base")
    up = _alembic("upgrade", "head")
    if up.returncode != 0:
        pytest.exit(
            "alembic upgrade head failed on the test DB:\n"
            f"STDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}",
            returncode=1,
        )

    yield


@pytest.fixture(autouse=True)
def _isolated_environment():
    """Function-scoped isolation: a clean data slate per test.

    TRUNCATE clears both relational rows and the co-located embeddings in one
    shot and resets BIGSERIAL identities so ids stay deterministic. The HNSW
    ANN index persists (correct). Cached provider/store singletons are reset so
    nothing leaks across tests.
    """
    _reset_service_caches()
    with db_session.engine.begin() as conn:
        conn.execute(text("TRUNCATE chunks, documents RESTART IDENTITY CASCADE"))

    yield

    _reset_service_caches()


@pytest.fixture()
def db():
    """Yield a database session bound to the test engine."""
    session = db_session.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def anon_client():
    """An *unauthenticated* FastAPI TestClient (no Authorization header).

    Use this for asserting 401s on protected routes and for exercising the
    public endpoints (``/health``, ``/chat/*``, ``/search``, ``POST /auth/login``)
    without credentials.

    NOTE: ``TestClient`` as a context manager runs the app lifespan, which calls
    ``init_db()``. The schema is already created by the session fixture's Alembic
    upgrade, and ``create_all`` is idempotent (a no-op against the migrated DB),
    so this is safe and deterministic.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _login_admin(test_client) -> str:
    """POST form-encoded admin creds to ``/auth/login`` and return the token.

    Credentials come from the *live* app settings (``get_settings()``) so this
    tracks the dev defaults (``admin`` / ``admin123``) without duplicating them.
    """
    from app.core.config import get_settings

    creds = get_settings()
    resp = test_client.post(
        "/auth/login",
        data={"username": creds.ADMIN_USERNAME, "password": creds.ADMIN_PASSWORD},
    )
    assert (
        resp.status_code == 200
    ), f"admin login failed in test setup: {resp.status_code} {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture()
def client():
    """A FastAPI TestClient pre-authenticated as the admin.

    Most tests hit now-protected routes (``/documents*``, ``/admin/*``), so the
    default client carries an admin ``Authorization: Bearer <token>`` header on
    every request. Public-endpoint tests work fine through it too. Use
    ``anon_client`` when you specifically need an unauthenticated client.

    NOTE: ``TestClient`` as a context manager runs the app lifespan, which calls
    ``init_db()``. The schema is already created by the session fixture's Alembic
    upgrade, and ``create_all`` is idempotent (a no-op against the migrated DB),
    so this is safe and deterministic.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        token = _login_admin(test_client)
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield test_client


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------
def upload_file(client, name: str, content: str, content_type: str = "text/plain"):
    """Upload an in-memory text file via the documents API. Returns the response."""
    return client.post(
        "/documents/upload",
        files={"file": (name, content.encode("utf-8"), content_type)},
    )


# A known leave-policy document used by search/chat/indexing tests. Contains the
# load-bearing keywords ("HR portal", "approval") the chat test asserts on.
LEAVE_POLICY_NAME = "leave_policy.txt"
LEAVE_POLICY_TEXT = (
    "Employee Leave Policy. "
    "All employees are entitled to twenty days of paid annual leave each year. "
    "To request leave, submit your application through the HR portal. "
    "Every leave request requires manager approval before it is granted. "
    "Sick leave does not require prior approval but must be reported to HR. "
    "Unused annual leave may be carried over to the next calendar year."
)
