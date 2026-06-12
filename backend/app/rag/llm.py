"""LLM provider abstraction and concrete implementations for answer generation.

Providers are selected via ``settings.LLM_PROVIDER``:

- :class:`LocalExtractiveLLMProvider` (default, no API key) — composes a grounded
  answer purely by extracting the most relevant sentences from the retrieved
  contexts that were embedded into the prompt. Fully deterministic and never
  hallucinates beyond the supplied context.
- :class:`OpenAILLMProvider` — OpenAI chat completions over httpx.
- :class:`AnthropicLLMProvider` — Anthropic Messages API over httpx.

The :func:`get_llm_provider` factory falls back to the local extractive provider
(with a logged warning) whenever the chosen cloud provider has no API key.

Cloud providers apply a short exponential backoff on rate limits; the calling
service is responsible for falling back to the local provider on persistent
failure so the endpoint never returns a 500 for provider issues.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator

from app.core.config import settings
from app.utils.errors import ProviderError, RateLimitError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Retry/backoff tuning for rate-limited cloud calls. The Gemini free tier returns
# 429 readily (low RPM), so we retry a few times with exponential backoff before
# the caller falls back to the local extractive provider.
_MAX_RETRIES = 4
_BACKOFF_BASE_SECONDS = 1.0

# Sentence splitter for the local extractive provider.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Leading interrogative ("Wh"/auxiliary) words that, together with a trailing
# '?' or an enumerated "N. ..." prefix, mark a line as a QUESTION rather than a
# declarative answer. Used by the local extractive provider to skip sentences
# that merely echo the user's question (e.g. a page-2 "RAG Test Questions" list
# chunk that landed in context) instead of answering it.
_INTERROGATIVE_WORDS: frozenset[str] = frozenset(
    {"who", "what", "when", "where", "why", "how", "which", "whom", "whose",
     "is", "are", "do", "does", "can", "could", "will", "would", "should",
     "did", "was", "were", "shall"}
)
# Enumerated list prefix, e.g. "1. Who is the CEO?" / "3) What is ...".
_ENUM_PREFIX_RE = re.compile(r"^\s*\d+[.)]\s")

_NO_CONTEXT_MESSAGE = "I couldn't find relevant information in the knowledge base."


def _chunk_words(text: str) -> Iterator[str]:
    """Yield ``text`` word-by-word so the concatenation reconstructs it exactly.

    Splits on whitespace and re-joins a single space between words, yielding one
    word (plus its following space, except for the last) at a time. This gives a
    deterministic multi-delta stream whose joined chunks equal the normalized
    text. Used as the default/fallback streaming strategy for providers that have
    no native token stream.
    """
    words = text.split()
    if not words:
        return
    last = len(words) - 1
    for index, word in enumerate(words):
        yield word if index == last else word + " "


class LLMProvider(ABC):
    """Abstract LLM provider producing a text answer from a prompt."""

    @abstractmethod
    def generate(self, prompt: str, system: str | None = None) -> str:
        """Generate an answer for ``prompt`` with an optional ``system`` prompt."""

    def generate_stream(self, prompt: str, system: str | None = None) -> Iterator[str]:
        """Stream the answer for ``prompt`` as a sequence of text chunks.

        The default implementation computes the full answer via
        :meth:`generate` and yields it word-by-word, so any provider that does
        not override this method still produces a real multi-delta stream.
        Concatenating all yielded chunks reconstructs the (whitespace-normalized)
        answer. Subclasses with a native token stream override this method.
        """
        yield from _chunk_words(self.generate(prompt, system))


class LocalExtractiveLLMProvider(LLMProvider):
    """Deterministic, dependency-light extractive "LLM".

    Rather than calling any external model, this provider composes a grounded
    answer by ranking the sentences contained in the prompt's context block by
    lexical overlap with the question and quoting the best ones. It never emits
    content that is not present in the prompt, so it cannot hallucinate.

    It parses the structured user prompt produced by
    :func:`app.rag.prompt.build_rag_prompt` (the ``Context excerpts:`` /
    ``Question:`` layout). If no context is present it returns a polite
    not-found message.
    """

    _MAX_SENTENCES = 3
    _MAX_ANSWER_CHARS = 800

    def generate(self, prompt: str, system: str | None = None) -> str:
        question = self._extract_question(prompt)
        context = self._extract_context(prompt)

        if not context.strip() or "No context excerpts were retrieved" in context:
            return _NO_CONTEXT_MESSAGE

        sentences = self._split_sentences(context)
        if not sentences:
            return _NO_CONTEXT_MESSAGE

        # Drop sentences that are themselves questions / near-duplicates of the
        # user's question. When the only context is a question-list chunk (e.g.
        # the page-2 "RAG Test Questions" list), every sentence is interrogative,
        # so this leaves nothing declarative and we abstain below instead of
        # echoing the question back as the "answer".
        declarative = [s for s in sentences if not self._is_question_like(s, question)]
        if not declarative:
            # Nothing groundable remains after removing question-echo lines.
            return _NO_CONTEXT_MESSAGE

        ranked = self._rank_sentences(declarative, question)
        if not ranked:
            # No declarative sentence shares any content term with the question.
            # The only "context" is off-topic prose / list scaffolding (headings,
            # enumeration markers), so abstain rather than quote noise back.
            return _NO_CONTEXT_MESSAGE

        answer = " ".join(ranked[: self._MAX_SENTENCES]).strip()
        if not answer:
            return _NO_CONTEXT_MESSAGE
        if len(answer) > self._MAX_ANSWER_CHARS:
            answer = answer[: self._MAX_ANSWER_CHARS].rstrip() + "..."

        documents = self._extract_document_names(context)
        if documents:
            answer = f"{answer}\n\n(Source: {', '.join(documents)})"
        return answer

    def generate_stream(self, prompt: str, system: str | None = None) -> Iterator[str]:
        """Stream the deterministic extractive answer word-by-word.

        Reuses :meth:`generate` for the exact same answer, then emits it
        progressively while preserving the original whitespace so the
        concatenation of all yielded chunks equals the full answer (including the
        trailing ``(Source: ...)`` line).
        """
        answer = self.generate(prompt, system)
        # Split on whitespace but keep the separators so spacing/newlines (e.g.
        # the "\n\n(Source: ...)" block) are reproduced exactly on re-join.
        for token in re.split(r"(\s+)", answer):
            if token:
                yield token

    @staticmethod
    def _extract_question(prompt: str) -> str:
        match = re.search(r"Question:\s*(.+)", prompt)
        return match.group(1).strip() if match else prompt.strip()

    @staticmethod
    def _extract_context(prompt: str) -> str:
        """Return the text between ``Context excerpts:`` and ``Question:``."""
        start = prompt.find("Context excerpts:")
        if start == -1:
            # Fall back to everything before the Question marker.
            end = prompt.find("Question:")
            return prompt[:end] if end != -1 else prompt
        start += len("Context excerpts:")
        end = prompt.find("Question:", start)
        return prompt[start:end] if end != -1 else prompt[start:]

    @staticmethod
    def _split_sentences(context: str) -> list[str]:
        sentences: list[str] = []
        for raw_line in context.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("[") or line.startswith("Document:"):
                # Skip the index/document-name header lines.
                line = re.sub(r"^\[\d+\]\s*Document:.*$", "", line).strip()
                if not line:
                    continue
            for part in _SENTENCE_RE.split(line):
                cleaned = part.strip()
                if cleaned:
                    sentences.append(cleaned)
        return sentences

    @staticmethod
    def _is_question_like(sentence: str, question: str) -> bool:
        """True when ``sentence`` is interrogative or echoes the user's question.

        A sentence is treated as a non-answer (to be skipped) when it is itself a
        question — it ends with '?', is an enumerated "N. <Wh>..." list line, or
        begins with a Wh/auxiliary interrogative word — OR when it is a near-
        duplicate of the user's own question (very high token overlap and a
        near-identical length). This stops the provider from quoting back a
        question-list chunk's lines (e.g. "1. Who is the CEO?") instead of the
        declarative answer ("CEO: Ananya Sharma"). It never drops a genuine
        declarative answer line such as "CEO: Ananya Sharma" or "Hotel
        reimbursement limit: ₹5000 per night".
        """
        text = (sentence or "").strip()
        if not text:
            return True
        lowered = text.lower()
        first = (_WORD_RE.findall(lowered) or [""])[0]
        # Enumerated "N. Who/What ..." line or any line starting with a Wh/aux
        # word, or any line ending in a question mark, is interrogative.
        if _ENUM_PREFIX_RE.match(text):
            # Strip the enumeration prefix and re-test the leading word so that
            # "1. Who is the CEO?" is caught but "1. Annual 24 days" is not.
            rest = _ENUM_PREFIX_RE.sub("", text, count=1)
            rest_first = (_WORD_RE.findall(rest.lower()) or [""])[0]
            if text.endswith("?") or rest_first in _INTERROGATIVE_WORDS:
                return True
        if text.endswith("?"):
            return True
        if first in _INTERROGATIVE_WORDS and " " in text:
            return True
        # Near-duplicate of the user's question: the sentence and the question
        # share almost the SAME token set (symmetric / Jaccard overlap), not
        # merely "all query tokens appear in the sentence" — otherwise a short
        # declarative answer like "CEO: Ananya Sharma" (which contains the query's
        # only content term "ceo") would be wrongly dropped for a "...CEO...?"
        # query. A genuine echo (e.g. the line "Who is the CEO?") has a near-
        # identical token set AND a near-identical length.
        s_terms = set(_WORD_RE.findall(lowered))
        q_terms = set(_WORD_RE.findall((question or "").lower()))
        if q_terms and s_terms:
            jaccard = len(q_terms & s_terms) / len(q_terms | s_terms)
            len_ratio = min(len(text), len(question)) / max(len(text), len(question), 1)
            if jaccard >= 0.8 and len_ratio >= 0.7:
                return True
        return False

    @staticmethod
    def _rank_sentences(sentences: list[str], question: str) -> list[str]:
        q_terms = set(_WORD_RE.findall(question.lower()))
        if not q_terms:
            return []
        scored: list[tuple[int, int, str]] = []
        for order, sentence in enumerate(sentences):
            terms = set(_WORD_RE.findall(sentence.lower()))
            overlap = len(q_terms & terms)
            if overlap > 0:
                # Sort by overlap desc, then original order asc (stable, readable).
                scored.append((-overlap, order, sentence))
        scored.sort()
        return [sentence for _, _, sentence in scored]

    @staticmethod
    def _extract_document_names(context: str) -> list[str]:
        names: list[str] = []
        for match in re.finditer(r"Document:\s*(.+)", context):
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
        return names


class OpenAILLMProvider(LLMProvider):
    """OpenAI chat completions provider via httpx."""

    _MODEL = "gpt-4o-mini"
    _URL = "https://api.openai.com/v1/chat/completions"
    _TIMEOUT = 60.0

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("OpenAILLMProvider requires an API key")
        self._api_key = api_key
        self._model = model or self._MODEL

    def generate(self, prompt: str, system: str | None = None) -> str:

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.0,
        }

        response = _post_with_retry(self._URL, headers, payload, self._TIMEOUT, "OpenAI")
        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("Unexpected OpenAI response shape: %s", exc)
            raise ProviderError("OpenAI returned an unexpected response") from exc

    def generate_stream(self, prompt: str, system: str | None = None) -> Iterator[str]:
        """Stream OpenAI chat completion content deltas (SSE, ``stream=True``).

        Parses the ``data:`` SSE lines, extracting ``choices[0].delta.content``
        from each event and yielding it as it arrives, stopping on ``[DONE]``. On
        ANY error (transport, status, parse, rate limit) it falls back to
        yielding the non-streaming :meth:`generate` answer in word chunks; this
        generator never raises out to the caller.
        """
        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.0,
            "stream": True,
        }

        yielded_any = False
        try:
            with httpx.Client(timeout=self._TIMEOUT) as client:
                with client.stream("POST", self._URL, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise ProviderError(f"OpenAI error (status {response.status_code})")
                    for line in response.iter_lines():
                        delta = _parse_openai_sse_line(line)
                        if delta is None:
                            continue
                        if delta == _SSE_DONE:
                            break
                        if delta:
                            yielded_any = True
                            yield delta
            if yielded_any:
                return
        except Exception as exc:  # noqa: BLE001 - generator must never raise.
            logger.warning(
                "OpenAI streaming failed (%s); falling back to non-streamed answer.",
                exc,
            )

        if not yielded_any:
            try:
                yield from _chunk_words(self.generate(prompt, system))
            except Exception as exc:  # noqa: BLE001 - never raise from a stream.
                logger.error("OpenAI fallback generate failed: %s", exc)


class AnthropicLLMProvider(LLMProvider):
    """Anthropic Messages API provider via httpx."""

    _MODEL = "claude-opus-4-8"
    _URL = "https://api.anthropic.com/v1/messages"
    _API_VERSION = "2023-06-01"
    _TIMEOUT = 60.0
    _MAX_TOKENS = 1024

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("AnthropicLLMProvider requires an API key")
        self._api_key = api_key
        self._model = model or self._MODEL

    def generate(self, prompt: str, system: str | None = None) -> str:

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._API_VERSION,
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._MAX_TOKENS,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        response = _post_with_retry(self._URL, headers, payload, self._TIMEOUT, "Anthropic")
        try:
            data = response.json()
            parts = data.get("content", [])
            text = "".join(
                str(part.get("text", "")) for part in parts if part.get("type") == "text"
            )
            return text.strip()
        except (AttributeError, ValueError) as exc:
            logger.error("Unexpected Anthropic response shape: %s", exc)
            raise ProviderError("Anthropic returned an unexpected response") from exc

    def generate_stream(self, prompt: str, system: str | None = None) -> Iterator[str]:
        """Stream Anthropic Messages text deltas (SSE, ``stream=True``).

        Parses the SSE event stream and yields the ``text`` from each
        ``content_block_delta`` event as it arrives. On ANY error it falls back
        to yielding the non-streaming :meth:`generate` answer in word chunks;
        this generator never raises out to the caller.
        """
        import httpx

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._API_VERSION,
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._MAX_TOKENS,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system:
            payload["system"] = system

        yielded_any = False
        try:
            with httpx.Client(timeout=self._TIMEOUT) as client:
                with client.stream("POST", self._URL, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise ProviderError(f"Anthropic error (status {response.status_code})")
                    for line in response.iter_lines():
                        text = _parse_anthropic_sse_line(line)
                        if text:
                            yielded_any = True
                            yield text
            if yielded_any:
                return
        except Exception as exc:  # noqa: BLE001 - generator must never raise.
            logger.warning(
                "Anthropic streaming failed (%s); falling back to " "non-streamed answer.",
                exc,
            )

        if not yielded_any:
            try:
                yield from _chunk_words(self.generate(prompt, system))
            except Exception as exc:  # noqa: BLE001 - never raise from a stream.
                logger.error("Anthropic fallback generate failed: %s", exc)


class GeminiLLMProvider(LLMProvider):
    """Google Gemini provider via the Generative Language REST API over httpx.

    Targets the free-tier models served at ``generativelanguage.googleapis.com``
    (e.g. ``gemini-2.0-flash``). The API key is sent in the ``x-goog-api-key``
    header rather than the URL query string to keep it out of logs.
    """

    _MODEL = "gemini-2.5-flash"
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    _TIMEOUT = 60.0
    _MAX_TOKENS = 1024

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("GeminiLLMProvider requires an API key")
        self._api_key = api_key
        self._model = model or self._MODEL

    def _url(self, method: str, *, sse: bool = False) -> str:
        suffix = "?alt=sse" if sse else ""
        return f"{self._BASE}/{self._model}:{method}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-goog-api-key": self._api_key}

    def _payload(self, prompt: str, system: str | None) -> dict[str, object]:
        payload: dict[str, object] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": self._MAX_TOKENS,
                # Disable "thinking" on 2.5-flash-class models: a RAG answer needs
                # no internal reasoning budget, and leaving it on can consume the
                # entire output budget on thoughts and return empty text.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def generate(self, prompt: str, system: str | None = None) -> str:
        response = _post_with_retry(
            self._url("generateContent"),
            self._headers(),
            self._payload(prompt, system),
            self._TIMEOUT,
            "Gemini",
        )
        try:
            data = response.json()
            return _gemini_extract_text(data).strip()
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.error("Unexpected Gemini response shape: %s", exc)
            raise ProviderError("Gemini returned an unexpected response") from exc

    def generate_stream(self, prompt: str, system: str | None = None) -> Iterator[str]:
        """Stream Gemini text deltas (SSE via ``:streamGenerateContent?alt=sse``).

        Each SSE ``data:`` line is a partial ``GenerateContentResponse``; we yield
        the concatenated text of its first candidate's parts. On ANY error it
        falls back to the non-streaming :meth:`generate` answer in word chunks;
        this generator never raises out to the caller.
        """
        import httpx

        yielded_any = False
        try:
            with httpx.Client(timeout=self._TIMEOUT) as client:
                with client.stream(
                    "POST",
                    self._url("streamGenerateContent", sse=True),
                    headers=self._headers(),
                    json=self._payload(prompt, system),
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise ProviderError(f"Gemini error (status {response.status_code})")
                    for line in response.iter_lines():
                        text = _parse_gemini_sse_line(line)
                        if text:
                            yielded_any = True
                            yield text
            if yielded_any:
                return
        except Exception as exc:  # noqa: BLE001 - generator must never raise.
            logger.warning(
                "Gemini streaming failed (%s); falling back to non-streamed answer.",
                exc,
            )

        if not yielded_any:
            try:
                yield from _chunk_words(self.generate(prompt, system))
            except Exception as exc:  # noqa: BLE001 - never raise from a stream.
                logger.error("Gemini fallback generate failed: %s", exc)


def _gemini_extract_text(data: object) -> str:
    """Concatenate the text parts of a Gemini response's first candidate.

    Does NOT strip: streaming deltas must preserve boundary whitespace so the
    concatenated stream reads correctly (e.g. ``"Female"`` + ``" employees"``).
    Callers that want a trimmed full answer (the non-streaming path) strip the
    final result themselves.
    """
    if not isinstance(data, dict):
        return ""
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))


def _parse_gemini_sse_line(line: str) -> str:
    """Parse one Gemini SSE line, returning the text delta (``""`` if none)."""
    payload = _sse_data_payload(line)
    if payload is None:
        return ""
    try:
        event = json.loads(payload)
    except ValueError:
        return ""
    return _gemini_extract_text(event)


# Sentinel returned by the OpenAI SSE parser when the terminal [DONE] is seen.
_SSE_DONE = "\x00__SSE_DONE__\x00"


def _sse_data_payload(line: str) -> str | None:
    """Return the JSON payload string of an SSE ``data:`` line, else ``None``."""
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


def _parse_openai_sse_line(line: str) -> str | None:
    """Parse one OpenAI SSE line.

    Returns the content delta string, an empty string for events with no
    content, :data:`_SSE_DONE` at the terminal ``[DONE]`` marker, or ``None`` for
    non-data/blank/unparseable lines (which the caller should skip).
    """
    payload = _sse_data_payload(line)
    if payload is None:
        return None
    if payload == "[DONE]":
        return _SSE_DONE
    try:
        event = json.loads(payload)
        return str(event["choices"][0]["delta"].get("content") or "")
    except (KeyError, IndexError, ValueError, TypeError):
        return ""


def _parse_anthropic_sse_line(line: str) -> str:
    """Parse one Anthropic SSE line, returning the text delta (``""`` if none)."""
    payload = _sse_data_payload(line)
    if payload is None:
        return ""
    try:
        event = json.loads(payload)
    except ValueError:
        return ""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    return str(delta.get("text") or "")


def _post_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
    provider_name: str,
):
    """POST with short exponential backoff on 429, mapping errors to exceptions.

    Raises:
        RateLimitError: when the provider keeps returning 429 after retries.
        ProviderError: for transport failures or other 4xx/5xx responses.
    """
    import httpx

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            logger.error("%s request failed: %s", provider_name, exc)
            raise ProviderError(f"{provider_name} request failed") from exc

        if response.status_code == 429:
            last_exc = RateLimitError(f"{provider_name} rate limit exceeded")
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "%s rate limited (attempt %s); retrying in %.1fs",
                    provider_name,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
                continue
            raise last_exc

        if response.status_code >= 400:
            logger.error(
                "%s error %s: %s",
                provider_name,
                response.status_code,
                response.text[:500],
            )
            raise ProviderError(f"{provider_name} error (status {response.status_code})")

        return response

    # Defensive: only reached if the loop exits without returning/raising.
    raise last_exc or ProviderError(f"{provider_name} request failed")


def get_llm_provider() -> LLMProvider:
    """Select an LLM provider from config with defensive local fallback.

    - ``local`` (default): :class:`LocalExtractiveLLMProvider`.
    - ``openai``: :class:`OpenAILLMProvider` if ``OPENAI_API_KEY`` is set,
      otherwise a warning is logged and the local provider is used.
    - ``anthropic``: :class:`AnthropicLLMProvider` if ``ANTHROPIC_API_KEY`` is
      set, otherwise a warning is logged and the local provider is used.
    - any unknown value: warns and uses the local provider.
    """
    provider = (settings.LLM_PROVIDER or "local").strip().lower()

    if provider == "openai":
        if settings.OPENAI_API_KEY:
            logger.info("Using OpenAI LLM provider")
            return OpenAILLMProvider(api_key=settings.OPENAI_API_KEY)
        logger.warning(
            "LLM_PROVIDER=openai but OPENAI_API_KEY is missing; "
            "falling back to the local extractive provider."
        )
        return LocalExtractiveLLMProvider()

    if provider == "anthropic":
        if settings.ANTHROPIC_API_KEY:
            logger.info("Using Anthropic LLM provider")
            return AnthropicLLMProvider(api_key=settings.ANTHROPIC_API_KEY)
        logger.warning(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is missing; "
            "falling back to the local extractive provider."
        )
        return LocalExtractiveLLMProvider()

    if provider == "gemini":
        if settings.GEMINI_API_KEY:
            logger.info("Using Gemini LLM provider (model=%s)", settings.GEMINI_MODEL)
            return GeminiLLMProvider(
                api_key=settings.GEMINI_API_KEY, model=settings.GEMINI_MODEL
            )
        logger.warning(
            "LLM_PROVIDER=gemini but GEMINI_API_KEY is missing; "
            "falling back to the local extractive provider."
        )
        return LocalExtractiveLLMProvider()

    if provider != "local":
        logger.warning("Unknown LLM_PROVIDER=%r; falling back to local.", provider)

    logger.info("Using local extractive LLM provider")
    return LocalExtractiveLLMProvider()
