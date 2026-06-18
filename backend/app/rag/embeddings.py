"""Embedding provider abstraction and concrete implementations.

Four providers are available, selected via ``settings.EMBEDDING_PROVIDER``:

- :class:`LocalHashingEmbeddingProvider` (default) — deterministic, dependency-light
  hashing/n-gram embedder. No torch / sentence-transformers; uses only stdlib
  hashing (``blake2b``) plus numpy for the math. Deterministic across processes
  because it relies on ``blake2b`` (NOT Python's salted ``hash()``).
- :class:`OpenAIEmbeddingProvider` — calls the OpenAI embeddings API over httpx.
- :class:`GeminiEmbeddingProvider` — calls Google's Generative Language
  embeddings API (``gemini-embedding-001``) over httpx, requesting a 384-dim
  output so it drops straight into the existing ``Vector(384)`` column. Unlike
  the local hashing embedder, it produces genuinely multilingual vectors
  (Hindi/Bengali queries match English source text), which is why it is the
  provider of choice for the multilingual scorecard.
- :class:`AnthropicCompatibleEmbeddingProvider` — Anthropic has **no public
  embeddings API**. Rather than failing hard, this provider logs a warning and
  delegates to the local hashing provider so the app always runs.

The :func:`get_embedding_provider` factory chooses by config and defensively falls
back to the local provider whenever a required key is missing.
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod

import numpy as np

from app.core.config import settings
from app.utils.errors import ProviderError, RateLimitError
from app.utils.logging import get_logger

logger = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(ABC):
    """Abstract embedding provider.

    Concrete providers turn text into fixed-dimension float vectors. Vectors are
    expected to be comparable via cosine similarity.
    """

    @property
    @abstractmethod
    def dim(self) -> int:
        """Dimensionality of produced vectors."""

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (e.g. document chunks)."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""


class LocalHashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic hashing/n-gram embedder producing ``EMBEDDING_DIM`` vectors.

    Algorithm:

    1. Lowercase and tokenize into word tokens.
    2. Build unigrams and bigrams as features.
    3. Hash each feature with ``blake2b`` (stable across processes) into a bucket
       in ``[0, dim)``, accumulating term frequency (with a sign bit to reduce
       collision bias, the "hashing trick").
    4. L2-normalize the resulting vector.

    Because ``blake2b`` is unsalted and deterministic, identical text always maps
    to the identical vector in any process — required for reproducible tests.
    """

    def __init__(self, dim: int | None = None) -> None:
        self._dim = int(dim if dim is not None else settings.EMBEDDING_DIM)
        if self._dim <= 0:
            raise ValueError("Embedding dimension must be positive")

    @property
    def dim(self) -> int:
        return self._dim

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return _WORD_RE.findall((text or "").lower())

    def _features(self, text: str) -> list[str]:
        tokens = self._tokenize(text)
        features: list[str] = list(tokens)
        # Bigrams capture a little local word order.
        for first, second in zip(tokens, tokens[1:], strict=False):
            features.append(f"{first}_{second}")
        return features

    @staticmethod
    def _hash_bucket(feature: str, dim: int) -> tuple[int, float]:
        """Return a stable (bucket, sign) pair for a feature via blake2b."""
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        bucket = value % dim
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return bucket, sign

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self._dim, dtype=np.float64)
        for feature in self._features(text):
            bucket, sign = self._hash_bucket(feature, self._dim)
            vector[bucket] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector.astype(np.float32).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embeddings provider (``text-embedding-3-small``) via httpx.

    Only instantiate this when ``OPENAI_API_KEY`` is present. ``text-embedding-3-small``
    natively produces 1536-dim vectors, but the API supports a ``dimensions``
    parameter so we request exactly ``settings.EMBEDDING_DIM`` to stay consistent
    with the vector store and the local fallback.
    """

    _MODEL = "text-embedding-3-small"
    _URL = "https://api.openai.com/v1/embeddings"
    _TIMEOUT = 30.0

    def __init__(self, api_key: str, dim: int | None = None) -> None:
        if not api_key:
            raise ValueError("OpenAIEmbeddingProvider requires an API key")
        self._api_key = api_key
        self._dim = int(dim if dim is not None else settings.EMBEDDING_DIM)

    @property
    def dim(self) -> int:
        return self._dim

    def _request(self, inputs: list[str]) -> list[list[float]]:
        import httpx

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._MODEL,
            "input": inputs,
            "dimensions": self._dim,
        }
        try:
            response = httpx.post(self._URL, headers=headers, json=payload, timeout=self._TIMEOUT)
        except httpx.HTTPError as exc:
            logger.error("OpenAI embeddings request failed: %s", exc)
            raise ProviderError("OpenAI embeddings request failed") from exc

        if response.status_code == 429:
            raise RateLimitError("OpenAI embeddings rate limit exceeded")
        if response.status_code >= 400:
            logger.error(
                "OpenAI embeddings error %s: %s",
                response.status_code,
                response.text[:500],
            )
            raise ProviderError(f"OpenAI embeddings error (status {response.status_code})")

        data = response.json().get("data", [])
        # The API preserves input order, but sort defensively by index.
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in ordered]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._request(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._request([text])[0]


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Google Gemini embeddings provider via the Generative Language REST API.

    Calls ``gemini-embedding-001:embedContent`` over httpx, mirroring
    :class:`app.rag.llm.GeminiLLMProvider` for auth (the API key rides in the
    ``x-goog-api-key`` header, kept out of the URL/logs), base URL and the short
    exponential backoff on 429.

    Dimensionality: the model natively emits 3072-dim vectors, but the API's
    ``outputDimensionality`` parameter lets us request exactly
    ``settings.EMBEDDING_DIM`` (384). That keeps the existing pgvector
    ``Vector(384)`` column intact — NO migration. Gemini does not L2-normalize
    truncated outputs itself, so we re-normalize each vector here to keep cosine
    similarity well-behaved (consistent with the other providers).

    Task types: chunk embeddings use ``RETRIEVAL_DOCUMENT`` and query embeddings
    use ``RETRIEVAL_QUERY``, the asymmetric pairing Gemini recommends for
    retrieval. The API embeds one content per request, so a batch is issued as a
    sequence of single calls.

    Rate limiting: the free tier caps embedContent at ~15 RPM, so a large PDF
    (hundreds of chunks) issued back-to-back 429s the whole job. Every request is
    therefore paced to a configurable RPM budget (``GEMINI_EMBED_RPM``, default 10
    — safely under the cap) via a per-INSTANCE monotonic last-call timestamp, so
    ``embed_texts`` and ``embed_query`` share one global budget. The 429s that
    still slip through get stronger exponential backoff (``GEMINI_EMBED_MAX_RETRIES``
    attempts, each sleep clamped to ``GEMINI_EMBED_MAX_BACKOFF_SECONDS``) that
    honors any server-provided ``Retry-After`` header or ``retryDelay`` body field.

    On a persistent 429 the provider raises :class:`RateLimitError` rather than
    silently returning anything — a hash-vector fallback would mix incompatible
    embedding spaces and quietly destroy retrieval, so the caller must see the
    error instead.
    """

    _MODEL = "gemini-embedding-001"
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    _TIMEOUT = 30.0
    # Module-level defensive defaults. The provider reads the matching Settings
    # values at construction and falls back to these if a setting is absent.
    _DEFAULT_RPM = 10
    _DEFAULT_MAX_RETRIES = 6
    _DEFAULT_BACKOFF_BASE_SECONDS = 0.5
    _DEFAULT_MAX_BACKOFF_SECONDS = 60.0

    def __init__(
        self,
        api_key: str,
        dim: int | None = None,
        *,
        rpm: int | None = None,
        max_retries: int | None = None,
        backoff_base_seconds: float | None = None,
        max_backoff_seconds: float | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiEmbeddingProvider requires an API key")
        self._api_key = api_key
        self._dim = int(dim if dim is not None else settings.EMBEDDING_DIM)

        # Config-driven knobs with defensive getattr fallbacks so a missing
        # setting never breaks the provider. Explicit constructor args win.
        rpm = rpm if rpm is not None else getattr(settings, "GEMINI_EMBED_RPM", self._DEFAULT_RPM)
        self._rpm = int(rpm) if rpm and int(rpm) > 0 else self._DEFAULT_RPM
        self._min_interval = 60.0 / self._rpm

        self._max_retries = int(
            max_retries
            if max_retries is not None
            else getattr(settings, "GEMINI_EMBED_MAX_RETRIES", self._DEFAULT_MAX_RETRIES)
        )
        self._backoff_base = float(
            backoff_base_seconds
            if backoff_base_seconds is not None
            else getattr(
                settings, "GEMINI_EMBED_BACKOFF_BASE_SECONDS", self._DEFAULT_BACKOFF_BASE_SECONDS
            )
        )
        self._max_backoff = float(
            max_backoff_seconds
            if max_backoff_seconds is not None
            else getattr(
                settings, "GEMINI_EMBED_MAX_BACKOFF_SECONDS", self._DEFAULT_MAX_BACKOFF_SECONDS
            )
        )

        # Per-INSTANCE monotonic timestamp of the last request. Initialized so the
        # very first call is not delayed: pretend the previous call happened a full
        # interval ago.
        self._last_request_ts: float = time.monotonic() - self._min_interval

    @property
    def dim(self) -> int:
        return self._dim

    def _pace(self) -> None:
        """Enforce the minimum inter-request interval (60/RPM seconds).

        Shared across embed_texts (document loop) and embed_query so every
        embedContent call draws from one global RPM budget per instance.
        """
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        # Stamp *after* any wait so spacing is measured between actual requests.
        self._last_request_ts = time.monotonic()

    @staticmethod
    def _parse_retry_after(response: object) -> float:
        """Extract a server-suggested retry delay (seconds) from a 429 response.

        Checks the HTTP ``Retry-After`` header (integer seconds) and the JSON
        error body's ``error.details[].retryDelay`` / top-level ``retryDelay``
        field (e.g. ``"7s"``). Returns ``0.0`` when nothing usable is present.
        """
        best = 0.0

        # HTTP Retry-After header (seconds form; HTTP-date form is ignored).
        headers = getattr(response, "headers", None)
        if headers:
            try:
                raw = headers.get("Retry-After") or headers.get("retry-after")
            except AttributeError:
                raw = None
            if raw:
                try:
                    best = max(best, float(str(raw).strip()))
                except (TypeError, ValueError):
                    pass

        # JSON error body retryDelay (Google returns e.g. {"retryDelay": "7s"}).
        try:
            body = response.json()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - body may be missing/non-JSON
            body = None

        def _coerce(value: object) -> float:
            if value is None:
                return 0.0
            text = str(value).strip().lower()
            if text.endswith("ms"):
                try:
                    return float(text[:-2]) / 1000.0
                except ValueError:
                    return 0.0
            if text.endswith("s"):
                text = text[:-1]
            try:
                return float(text)
            except ValueError:
                return 0.0

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                best = max(best, _coerce(error.get("retryDelay")))
                details = error.get("details")
                if isinstance(details, list):
                    for detail in details:
                        if isinstance(detail, dict):
                            best = max(best, _coerce(detail.get("retryDelay")))
            best = max(best, _coerce(body.get("retryDelay")))

        return best

    def _embed_one(self, text: str, task_type: str) -> list[float]:
        """Embed a single text with the given Gemini ``task_type``.

        Paces every request to the configured RPM budget, then retries with
        exponential backoff (clamped to the ceiling) on 429 while honoring any
        server-provided Retry-After. Raises on persistent rate limiting or any
        other API/transport error — never returns a fallback vector from an
        incompatible space.
        """
        import httpx

        url = f"{self._BASE}/{self._MODEL}:embedContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        payload = {
            "model": f"models/{self._MODEL}",
            "content": {"parts": [{"text": text or ""}]},
            "taskType": task_type,
            "outputDimensionality": self._dim,
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            # Pace BEFORE every attempt so the global RPM budget covers retries
            # too — back-to-back retries would otherwise themselves trip the cap.
            self._pace()
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=self._TIMEOUT)
            except httpx.HTTPError as exc:
                logger.error("Gemini embeddings request failed: %s", exc)
                raise ProviderError("Gemini embeddings request failed") from exc

            status = response.status_code
            # 429 (rate limit) AND transient server errors (500/502/503/504) are
            # retryable with the same paced backoff — Gemini intermittently returns
            # 503 "overloaded", which must not fail an otherwise-healthy long
            # indexing run. Non-transient 4xx (400/401/403/404) are NOT retried.
            if status == 429 or status in (500, 502, 503, 504):
                last_exc = (
                    RateLimitError("Gemini embeddings rate limit exceeded")
                    if status == 429
                    else ProviderError(f"Gemini embeddings error (status {status})")
                )
                if attempt < self._max_retries:
                    backoff = self._backoff_base * (2**attempt)
                    retry_after = self._parse_retry_after(response)
                    delay = min(max(backoff, retry_after), self._max_backoff)
                    logger.warning(
                        "Gemini embeddings transient error %s (attempt %s); retrying in %.1fs "
                        "(backoff=%.1fs retry_after=%.1fs)",
                        status,
                        attempt + 1,
                        delay,
                        backoff,
                        retry_after,
                    )
                    time.sleep(delay)
                    continue
                raise last_exc
            if status >= 400:
                logger.error(
                    "Gemini embeddings error %s: %s",
                    status,
                    response.text[:500],
                )
                raise ProviderError(
                    f"Gemini embeddings error (status {status})"
                )

            try:
                values = response.json()["embedding"]["values"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.error("Unexpected Gemini embeddings response shape: %s", exc)
                raise ProviderError("Gemini returned an unexpected embeddings response") from exc
            return self._normalize(values)

        # Defensive: only reached if the loop exits without returning/raising.
        raise last_exc or ProviderError("Gemini embeddings request failed")

    @staticmethod
    def _normalize(values: list[float]) -> list[float]:
        """L2-normalize a vector (truncated MRL outputs are not unit-norm)."""
        vector = np.asarray(values, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector = vector / norm
        return vector.astype(np.float32).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [self._embed_one(text, "RETRIEVAL_DOCUMENT") for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text, "RETRIEVAL_QUERY")


class AnthropicCompatibleEmbeddingProvider(EmbeddingProvider):
    """Anthropic-compatible embeddings provider.

    Anthropic does not currently expose a public embeddings API. To keep the app
    running rather than raising, this provider logs a warning and delegates every
    call to :class:`LocalHashingEmbeddingProvider`. This documented choice means
    selecting ``EMBEDDING_PROVIDER=anthropic`` degrades gracefully to the local
    embedder instead of failing hard.
    """

    def __init__(self, dim: int | None = None) -> None:
        logger.warning(
            "Anthropic has no public embeddings API; falling back to the local "
            "hashing embedder for EMBEDDING_PROVIDER=anthropic."
        )
        self._delegate = LocalHashingEmbeddingProvider(dim=dim)

    @property
    def dim(self) -> int:
        return self._delegate.dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._delegate.embed_texts(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._delegate.embed_query(text)


def get_embedding_provider() -> EmbeddingProvider:
    """Select an embedding provider from config with defensive local fallback.

    - ``local`` (default): :class:`LocalHashingEmbeddingProvider`.
    - ``openai``: :class:`OpenAIEmbeddingProvider` if ``OPENAI_API_KEY`` is set,
      otherwise a warning is logged and the local provider is used.
    - ``gemini``: :class:`GeminiEmbeddingProvider` if ``GEMINI_API_KEY`` is set,
      otherwise a warning is logged and the local provider is used (keyless boot).
    - ``anthropic``: :class:`AnthropicCompatibleEmbeddingProvider` (delegates to
      local).
    - any unknown value: warns and uses the local provider.
    """
    provider = (settings.EMBEDDING_PROVIDER or "local").strip().lower()

    if provider == "openai":
        if settings.OPENAI_API_KEY:
            logger.info("Using OpenAI embedding provider")
            return OpenAIEmbeddingProvider(api_key=settings.OPENAI_API_KEY)
        logger.warning(
            "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is missing; "
            "falling back to the local hashing embedder."
        )
        return LocalHashingEmbeddingProvider()

    if provider == "gemini":
        if settings.GEMINI_API_KEY:
            logger.info("Using Gemini embedding provider (dim=%s)", settings.EMBEDDING_DIM)
            return GeminiEmbeddingProvider(
                api_key=settings.GEMINI_API_KEY,
                rpm=getattr(settings, "GEMINI_EMBED_RPM", None),
                max_retries=getattr(settings, "GEMINI_EMBED_MAX_RETRIES", None),
                backoff_base_seconds=getattr(settings, "GEMINI_EMBED_BACKOFF_BASE_SECONDS", None),
                max_backoff_seconds=getattr(settings, "GEMINI_EMBED_MAX_BACKOFF_SECONDS", None),
            )
        logger.warning(
            "EMBEDDING_PROVIDER=gemini but GEMINI_API_KEY is missing; "
            "falling back to the local hashing embedder."
        )
        return LocalHashingEmbeddingProvider()

    if provider == "anthropic":
        return AnthropicCompatibleEmbeddingProvider()

    if provider != "local":
        logger.warning("Unknown EMBEDDING_PROVIDER=%r; falling back to local.", provider)

    logger.info("Using local hashing embedding provider (dim=%s)", settings.EMBEDDING_DIM)
    return LocalHashingEmbeddingProvider()
