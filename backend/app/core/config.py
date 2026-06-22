"""Application configuration via pydantic-settings.

A single cached ``Settings`` object is loaded from environment variables / ``.env``.
No secret is ever hardcoded; absence of API keys is a valid, supported state that
activates the local fallback providers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are read from environment variables / a ``.env`` file. Unknown keys are
    ignored so the same ``.env`` can be shared with other tooling.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---------------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg://raguser@127.0.0.1:54329/ragdb"

    # --- Vector store -----------------------------------------------------
    VECTOR_DB: str = "pgvector"

    # --- Providers --------------------------------------------------------
    EMBEDDING_PROVIDER: str = "local"
    LLM_PROVIDER: str = "local"
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    # Google Gemini (free tier via Google AI Studio: https://aistudio.google.com/apikey).
    GEMINI_API_KEY: str | None = None
    # Optional second Gemini key used as an automatic fallback when the primary
    # key hits its free-tier daily quota (HTTP 429 RESOURCE_EXHAUSTED). The
    # providers rotate to it instantly (no backoff) on a 429 from the primary.
    GEMINI_API_KEY_FALLBACK: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # --- Gemini embedding rate limiting -----------------------------------
    # The free tier embedContent endpoint embeds one chunk per request and caps
    # at ~15 requests/minute. An 80-page PDF (~240 chunks) issued back-to-back
    # blows past that and 429s the whole index job. We therefore pace requests to
    # a configurable RPM budget (kept safely under 15) and back off harder on the
    # 429s that still slip through, honoring server-provided Retry-After hints.
    GEMINI_EMBED_RPM: int = 10
    GEMINI_EMBED_MAX_RETRIES: int = 6
    GEMINI_EMBED_BACKOFF_BASE_SECONDS: float = 0.5
    GEMINI_EMBED_MAX_BACKOFF_SECONDS: float = 60.0
    # A paced 240-chunk doc at 10 RPM takes ~24 min, so the stale-processing sweep
    # must allow a generous margin or it would mark a healthy-but-slow job 'failed'
    # on a worker restart. Minutes after which a stuck 'processing' doc is failed.
    INDEXING_STALE_MINUTES: int = 120

    # --- RAG / embedding parameters --------------------------------------
    EMBEDDING_DIM: int = 384
    # Smaller chunks concentrate rare terms: with the IDF-free local hashing
    # embedder, a lone term (e.g. "transpose") in a 1000-char chunk is diluted
    # after L2 normalization. 500/100 gives rare-term definitions enough weight
    # to surface in vector search (a definition-only chunk then scores ~0.45 vs
    # ~0.21 when buried in a 1000-char chunk).
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 100
    TOP_K: int = 8

    # --- Cloudinary (remote document storage) ----------------------------
    # When all three are set, uploaded files are stored on Cloudinary instead of
    # the local uploads disk (Render free tier has no persistent disk). Unset =
    # local-disk fallback, identical to the original behavior.
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None

    # --- Uploads ----------------------------------------------------------
    # ``0`` (or any value <= 0) means *unlimited* — the size guard is skipped.
    MAX_UPLOAD_MB: int = 0
    # ``NoDecode`` disables pydantic-settings' eager JSON parsing of env values so
    # the CSV-aware validator below receives the raw string.
    ALLOWED_EXTENSIONS: Annotated[list[str], NoDecode] = ["pdf", "docx", "txt", "md"]

    # --- HTTP / CORS ------------------------------------------------------
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # --- Auth (single hardcoded admin; no users table) --------------------
    # Dev defaults let tests run with zero extra env. OVERRIDE IN PRODUCTION.
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    # Login email for the seeded admin user (used by the multi-tenant migration
    # seed). ADMIN_USERNAME ("admin") is not a valid email, so the admin logs in
    # with this address.
    ADMIN_EMAIL: str = "admin@local"
    # Gate for public self-registration (POST /auth/register).
    ALLOW_REGISTRATION: bool = True
    JWT_SECRET_KEY: str = "dev-secret-change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720

    # --- Employee Zone (SSO identity federation) --------------------------
    # ALL optional with safe defaults so existing boot/tests are unaffected.
    #
    # Master switch. When False (the CURRENT mode), the /employee/* endpoints
    # require NO token at all: identity is resolved from optional unverified
    # headers, falling back to the shared EZ_DEFAULT_EMPLOYEE_EMAIL. When True
    # (future), real Employee Zone access-token verification kicks in (see the
    # TODO seam in app.core.employee_zone).
    EZ_AUTH_ENABLED: bool = False
    # --- Reserved for future EZ token verification (not used yet) ---------
    # Verification mode selector once EZ_AUTH_ENABLED is True
    # (e.g. "introspection" / "jwks" / "hs256"); "disabled" = no verification.
    EZ_VERIFY_MODE: str = "disabled"
    EZ_ISSUER: str | None = None
    EZ_AUDIENCE: str | None = None
    EZ_JWKS_URL: str | None = None
    EZ_SHARED_SECRET: str | None = None
    EZ_INTROSPECTION_URL: str | None = None
    # Claim names used to extract identity from a verified EZ token.
    EZ_EMAIL_CLAIM: str = "email"
    EZ_EMPLOYEE_ID_CLAIM: str = "emp_id"
    EZ_NAME_CLAIM: str = "full_name"
    # The shared identity used when auth is disabled and the client sends no
    # X-Employee-Email header.
    EZ_DEFAULT_EMPLOYEE_EMAIL: str = "employee@local"

    @property
    def cloudinary_enabled(self) -> bool:
        """True only when all three Cloudinary credentials are configured."""
        return bool(
            self.CLOUDINARY_CLOUD_NAME
            and self.CLOUDINARY_API_KEY
            and self.CLOUDINARY_API_SECRET
        )

    @field_validator("ALLOWED_EXTENSIONS", "CORS_ORIGINS", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept either a real list or a comma-separated string from env.

        ``pydantic-settings`` reads env vars as plain strings, so a value like
        ``pdf,docx,txt`` must be parsed into a list. A JSON array string
        (``["pdf","docx"]``) is also accepted.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                import json

                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance (one per process)."""
    return Settings()


# Module-level convenience instance.
settings: Settings = get_settings()
