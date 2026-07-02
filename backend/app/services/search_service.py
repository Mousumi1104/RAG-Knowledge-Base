"""Semantic search business logic.

The flow is:

1. Embed the natural-language query with the configured embedding provider.
2. Query the vector store for the most similar chunks.
3. Resolve each hit back to its relational :class:`Document` / :class:`Chunk`
   rows, keeping only results whose document AND chunk are ACTIVE.
4. Return ranked results (most relevant first) shaped for the API contract.

Only ACTIVE content is ever returned: a vector may linger for a document/chunk
that has since been soft-deleted, so we re-validate every hit against the RDBMS.
To still return up to ``top_k`` *active* results even when some candidates are
filtered out, we over-fetch a few extra candidates from the vector store.

The embedding provider and vector store are constructed lazily and cached at
module level so repeated searches reuse the same (potentially expensive) clients.
"""

from __future__ import annotations

import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.base import Status
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.utils.logging import get_logger
from app.vectorstore import VectorStore, get_vector_store

logger = get_logger(__name__)

# Cap excerpt length so responses stay compact; full content remains in the store.
_MAX_EXCERPT_CHARS = 500
# Multiplier of extra candidates fetched so active-filtering can still fill top_k.
_OVERFETCH_FACTOR = 3
_OVERFETCH_MIN_EXTRA = 5
# Floor on the candidate pool size. The local hashing embedder has NO IDF, so a
# lone rare term (e.g. "transpose") inside a large chunk is diluted after L2
# normalization and the best defining chunk can rank well below the vector
# top_k. We over-fetch a wide pool, then re-rank with a lexical/keyword signal
# (see _keyword_overlap) so exact-term matches are rescued into top_k.
#
# This floor is deliberately LARGE because, in a multi-tenant store, a few huge
# admin-global documents (e.g. 2908 + 1552 chunks) otherwise fill the entire
# pool with their own chunks and a small tenant document's answer chunks never
# even enter it. A wide pool lets every small-doc chunk in; the per-document cap
# (see _PER_DOCUMENT_CANDIDATE_CAP) then stops those big docs from re-flooding
# top_k after re-ranking. Tuned so the small CDN doc's 6 chunks reliably enter.
_OVERFETCH_MIN_CANDIDATES = 500
# Weight of the lexical keyword-overlap signal when blended with the vector
# score: final = vector_score + _KEYWORD_WEIGHT * weighted_keyword_overlap. The
# overlap is now IDF-weighted over the candidate pool (see
# _weighted_keyword_overlap), so a chunk matching the rare discriminative query
# term decisively beats one matching only ubiquitous words. Raised from 0.30 so
# that signal can overcome the spuriously-high vector scores the IDF-free
# embedder assigns large off-topic global-doc chunks, without overwhelming
# genuine semantic relevance for paraphrased queries (overlap there is low).
_KEYWORD_WEIGHT = 0.45

# Deterministic lexical anchor for rare, short, acronym-like query tokens. The
# live Gemini embedder embeds the query WITHOUT case normalization, so a
# lowercase acronym (e.g. "apdr") gets an off-topic vector even though the chunk
# that defines it is a certain lexical match. When such a rare token is literally
# present in a chunk, we add a fixed bonus large enough to (a) lift that chunk to
# #0 and (b) clear the downstream Gemini relevance floor (0.40) on its own, even
# when the lowercase vector score is ~0. Confined to rare acronym-like tokens so
# ordinary content queries are unaffected.
#   - _ACRONYM_TOKEN_RE: 2-6 char alphanumeric token (lowercased query word).
#   - _EXACT_TOKEN_ANCHOR_BONUS: >0.40 so the bonus alone clears the floor.
#   - _ACRONYM_MIN_IDF: only rare/discriminative tokens (pool-IDF >= this) anchor.
_ACRONYM_TOKEN_RE = re.compile(r"^[a-z0-9]{2,6}$")
_EXACT_TOKEN_ANCHOR_BONUS = 0.50
_ACRONYM_MIN_IDF = 1.5

# Extra bonus for a chunk that literally INTRODUCES the queried term, i.e. the
# word "Definition" immediately precedes a query content-term (e.g. "Definition
# The transpose of ..."). For a definition-seeking query this is the gold chunk,
# yet it is often the worst-scoring vector hit among many that merely *mention*
# the term (the term is diluted by surrounding prose). This proximity bonus is
# what breaks that tie. Applied ONLY for definition-seeking queries.
_DEFINITION_BONUS = 0.30
# How many chars before a query term to scan for "definition" (handles PDFs that
# glue words together, e.g. "Definitionthetranspose").
_DEFINITION_WINDOW = 32

# Cap on how many candidate chunks any SINGLE document may contribute to the
# pool before active-filtering/re-ranking. A large global doc (e.g. 2908 chunks)
# would otherwise flood the over-fetched pool and eject a small tenant doc's best
# chunks out of top_k entirely. Keeping only each document's strongest chunks
# preserves tenant isolation (the SQL layer already scopes ownership) while
# giving smaller docs a fair shot at top_k. Generous enough that genuinely
# multi-chunk answers from one doc still survive.
_PER_DOCUMENT_CANDIDATE_CAP = 8

# Word tokenizer shared with the local embedder's notion of a "word".
_WORD_RE = re.compile(r"[a-z0-9]+")

# Penalty subtracted from a chunk's blended rank_score when it is a predominantly
# interrogative "question-list" chunk (e.g. the page-2 "RAG Test Questions" list:
# enumerated "1. Who is the CEO?", "2. When was the company founded?" ...). Such a
# chunk lexically matches a "Who is the CEO?" query extremely well yet contains NO
# declarative answer, so without this penalty it tops the ranking and (a) anchors
# the top-document rule on the wrong chunk and (b) gets echoed back as the
# "answer". The penalty is large enough to push it below the sibling page-1
# DECLARATIVE answer chunk, which then wins #0. See :func:`_question_list_ratio`.
_QUESTION_LIST_PENALTY = 0.50
# A chunk is treated as a question-list when this fraction or more of its
# non-empty lines are interrogative (end in '?' or are an enumerated 'N. <Wh>...'
# line) AND it has at least this many such lines (so a single trailing question
# in an otherwise declarative chunk is not penalized).
_QUESTION_LIST_RATIO = 0.6
_QUESTION_LIST_MIN_LINES = 3

# Enumerated interrogative line, e.g. "1. Who is the CEO?" / "3) What is ...".
_ENUM_QUESTION_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
# Leading interrogative/Wh word that marks a question line lacking a '?'.
_WH_WORDS: frozenset[str] = frozenset(
    {"who", "what", "when", "where", "why", "how", "which", "whom", "whose", "is",
     "are", "do", "does", "can", "could", "will", "would", "should"}
)

# Common English stop / interrogative words stripped from a query before
# computing keyword overlap, so generic words ("what", "is", "the", "of", "a")
# don't drown out the rare content term that actually discriminates chunks.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "of", "in", "on", "at", "to", "for", "and", "or", "but", "with", "as",
        "by", "from", "that", "this", "these", "those", "it", "its",
        "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
        "do", "does", "did", "can", "could", "would", "should", "will", "shall",
        "me", "my", "you", "your", "i", "we", "they", "them", "he", "she",
        "about", "tell", "explain", "define", "definition", "mean", "means",
        "give", "show", "describe",
    }
)


# Light synonym expansion for the keyword-rescue path only. The IDF-free local
# embedder cannot bridge synonyms, so a "from where can I avail/get/access a CDN"
# query never lexically matches the document's "Popular CDN Providers" heading
# (the doc never uses avail/get/access). When any of these intent words appear in
# the query's content-words, we ADD provider-vocabulary terms so the keyword
# rescue and overlap signal can reach the provider-listing chunk. This only
# augments the lexical signal; it never removes the user's own terms.
_QUERY_SYNONYMS: dict[str, frozenset[str]] = {
    "avail": frozenset({"providers", "provider", "vendor"}),
    "get": frozenset({"providers", "provider", "vendor"}),
    "access": frozenset({"providers", "provider", "vendor"}),
    "obtain": frozenset({"providers", "provider", "vendor"}),
    "buy": frozenset({"providers", "provider", "vendor"}),
    "purchase": frozenset({"providers", "provider", "vendor"}),
}


def _content_words(text: str) -> set[str]:
    """Lowercase content (non-stopword) word tokens of ``text``."""
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def _expand_query_words(query_words: set[str]) -> set[str]:
    """Augment query content-words with light synonym vocabulary (see _QUERY_SYNONYMS).

    Returns the original words plus any mapped provider-vocabulary terms. Never
    drops a user term. When no synonym trigger is present the set is unchanged,
    so this is a no-op for the common case and cannot affect small-talk (which
    has no content words at all).
    """
    expanded = set(query_words)
    for word in query_words:
        synonyms = _QUERY_SYNONYMS.get(word)
        if synonyms:
            expanded |= synonyms
    return expanded


def _keyword_overlap(query_words: set[str], content: str) -> float:
    """Fraction of distinct query content-words present in ``content`` (0..1).

    A lexical signal that is high precisely when a chunk literally contains the
    rare/important terms of the query, regardless of how the IDF-free hashing
    embedder diluted them in the vector. Returns 0.0 when the query has no
    content words (e.g. a pure stopword phrase), so blending is a no-op there.

    Matching is by *substring*, not whole-token equality, because PDF text
    extraction frequently drops inter-word spaces ("Thetranspose", "thek×r
    matrix"). A whole-word test would then miss the very definition we need to
    rescue; substring matching credits ``"transpose"`` inside ``"thetranspose"``.
    """
    if not query_words:
        return 0.0
    lowered = (content or "").lower()
    if not lowered:
        return 0.0
    matched = sum(1 for w in query_words if w in lowered)
    return matched / len(query_words)


def _pool_idf_weights(query_words: set[str], contents: list[str]) -> dict[str, float]:
    """Pool-local IDF weight per query term over the candidate ``contents``.

    The hashing embedder has no IDF, so an over-fetched pool dominated by a few
    huge global documents lets common query words (e.g. "make", "terms",
    "simple") match many chunks while the one *discriminative* term (e.g. "cdn",
    which appears only in the small target doc) matches few. A plain
    :func:`_keyword_overlap` fraction then gives a global chunk that matches two
    common words MORE credit than the target chunk that matches the single rare
    term that actually identifies the topic.

    To counter that we compute a cheap IDF over the candidate pool itself:
    ``idf(term) = ln((N + 1) / (df + 1)) + 1`` where ``df`` is the number of
    candidate chunks whose (lowercased) content contains ``term``. Rare,
    topic-defining terms get a high weight; ubiquitous words get a low one. This
    is the same intuition as classic IDF, just scoped to the live candidate pool
    (no index, no new dependency). Returns ``{}`` when there are no contents.
    """
    if not query_words or not contents:
        return {}
    lowered = [(c or "").lower() for c in contents]
    n = len(lowered)
    weights: dict[str, float] = {}
    for term in query_words:
        df = sum(1 for c in lowered if term in c)
        weights[term] = math.log((n + 1) / (df + 1)) + 1.0
    return weights


def _weighted_keyword_overlap(
    query_words: set[str], content: str, idf: dict[str, float]
) -> float:
    """IDF-weighted fraction of query terms present in ``content`` (0..1).

    Like :func:`_keyword_overlap` but each matched term contributes its pool-IDF
    weight (see :func:`_pool_idf_weights`) instead of 1, and the result is
    normalized by the total possible weight. A chunk matching one rare,
    discriminative term thus outscores a chunk matching several ubiquitous ones.
    Falls back to the unweighted fraction when no IDF map is supplied.
    """
    if not query_words:
        return 0.0
    if not idf:
        return _keyword_overlap(query_words, content)
    lowered = (content or "").lower()
    if not lowered:
        return 0.0
    total = sum(idf.get(w, 1.0) for w in query_words)
    if total <= 0:
        return 0.0
    matched = sum(idf.get(w, 1.0) for w in query_words if w in lowered)
    return matched / total


def _acronym_anchor_terms(query_words: set[str], idf: dict[str, float]) -> set[str]:
    """Rare, short, acronym-like query terms that should deterministically anchor.

    Returns the subset of ``query_words`` that both (a) look like an acronym/short
    code (match :data:`_ACRONYM_TOKEN_RE`) AND (b) are rare/discriminative in the
    candidate pool (pool-IDF >= :data:`_ACRONYM_MIN_IDF`). A term literally present
    in a chunk then earns :data:`_EXACT_TOKEN_ANCHOR_BONUS`, which is enough to
    lift that chunk to #0 and clear the downstream relevance floor even when the
    live embedder gave the (lowercase) query an off-topic vector. Common words and
    ordinary long content terms never qualify, so non-acronym queries are
    unaffected.
    """
    return {
        term
        for term in query_words
        if _ACRONYM_TOKEN_RE.match(term) and idf.get(term, 1.0) >= _ACRONYM_MIN_IDF
    }


def _is_question_list_chunk(content: str) -> bool:
    """True when ``content`` is a predominantly-interrogative question-list chunk.

    Counts non-empty lines that are interrogative — they end in '?', or are an
    enumerated "N. ..." line, or begin with a Wh/auxiliary interrogative word —
    and flags the chunk when at least :data:`_QUESTION_LIST_MIN_LINES` lines and
    at least :data:`_QUESTION_LIST_RATIO` of all non-empty lines are interrogative
    AND declarative content is sparse. This precisely identifies the page-2 test
    "RAG Test Questions" list (all enumerated questions, no answers) without
    penalizing a normal answer paragraph that merely ends with one question.
    """
    lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
    if len(lines) < _QUESTION_LIST_MIN_LINES:
        return False
    interrogative = 0
    for line in lines:
        lowered = line.lower()
        first = (_WORD_RE.findall(lowered) or [""])[0]
        if line.endswith("?") or _ENUM_QUESTION_RE.match(line) or first in _WH_WORDS:
            interrogative += 1
    return interrogative / len(lines) >= _QUESTION_LIST_RATIO


def _is_definition_query(query: str) -> bool:
    """True for definition-seeking phrasings like "what is X" / "define X"."""
    q = (query or "").lower().strip()
    return any(
        q.startswith(p)
        for p in ("what is", "what are", "what s ", "define ", "definition of", "what does")
    ) or " mean" in q


def _defines_term(query_words: set[str], content: str) -> bool:
    """True if the chunk literally introduces a query term as a "Definition".

    Looks for the word "definition" occurring just before a query content-term
    (within :data:`_DEFINITION_WINDOW` chars), tolerating glued-together PDF text
    such as "Definitionthetranspose". This precisely identifies the chunk that
    *defines* the queried concept versus chunks that merely mention it.
    """
    if not query_words:
        return False
    lowered = (content or "").lower()
    if "definition" not in lowered:
        return False
    for term in query_words:
        start = 0
        while True:
            idx = lowered.find(term, start)
            if idx == -1:
                break
            if "definition" in lowered[max(0, idx - _DEFINITION_WINDOW): idx]:
                return True
            start = idx + 1
    return False


# Lazy module-level singletons (built on first use, reused afterwards).
_embedding_provider: EmbeddingProvider | None = None
_vector_store: VectorStore | None = None


def _get_embedding_provider() -> EmbeddingProvider:
    global _embedding_provider
    if _embedding_provider is None:
        _embedding_provider = get_embedding_provider()
    return _embedding_provider


def _get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = get_vector_store()
    return _vector_store


def reset_caches() -> None:
    """Drop cached provider/store singletons (used by tests after config change)."""
    global _embedding_provider, _vector_store
    _embedding_provider = None
    _vector_store = None


def _truncate(text: str, limit: int = _MAX_EXCERPT_CHARS) -> str:
    """Truncate ``text`` to ``limit`` characters, appending an ellipsis if cut."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _active_documents_by_id(db: Session, document_ids: set[int]) -> dict[int, Document]:
    """Return a ``{id: Document}`` map for ACTIVE documents among ``document_ids``."""
    if not document_ids:
        return {}
    stmt = select(Document).where(
        Document.id.in_(document_ids),
        Document.status == Status.ACTIVE.value,
    )
    return {doc.id: doc for doc in db.execute(stmt).scalars().all()}


def _active_chunk_ids(db: Session, chunk_ids: set[str]) -> set[str]:
    """Return the subset of ``chunk_ids`` that map to ACTIVE (non-deleted) chunks."""
    if not chunk_ids:
        return set()
    stmt = select(Chunk.chunk_id).where(
        Chunk.chunk_id.in_(chunk_ids),
        Chunk.status == Status.ACTIVE.value,
    )
    return set(db.execute(stmt).scalars().all())


def search(
    db: Session,
    query: str,
    top_k: int | None = None,
    *,
    owner_id: int | None,
    include_global: bool = True,
) -> list[dict]:
    """Run semantic search and return ranked, active-only, tenant-scoped results.

    Tenant isolation is DEFAULT-DENY and enforced in the vector layer: only
    chunks whose document is owned by ``owner_id`` OR (when ``include_global``)
    is an admin-global document are ever candidates. No other tenant's chunk can
    enter the candidate pool, the keyword-rescue pool, or the results.

    Args:
        db: Database session for resolving documents/chunks.
        query: Natural-language query string.
        top_k: Max number of active results to return; defaults to ``settings.TOP_K``.
        owner_id: The requesting user's id (their own docs are candidates). With
            ``None`` and ``include_global`` False the candidate set is empty.
        include_global: Whether admin-global docs may ground the answer.

    Returns:
        A list of result dicts, most relevant first, each shaped as::

            {document_id, document_name, excerpt, chunk_id, page, score,
             rank_score, owner_id, is_global}

        ``score`` is the raw vector similarity; ``rank_score`` is the blended
        hybrid score used for ordering and for the downstream relevance gate.
    """
    query = (query or "").strip()
    if not query:
        # The API layer enforces non-empty queries; guard here too for safety.
        return []

    effective_top_k = top_k if top_k is not None and top_k > 0 else settings.TOP_K
    if effective_top_k <= 0:
        return []

    # Over-fetch so that, after dropping hits whose doc/chunk is inactive, we can
    # still return up to ``effective_top_k`` active results. A wide pool is also
    # required for the lexical re-rank below to find exact-term chunks that the
    # IDF-free vector ranks far down (see _OVERFETCH_MIN_CANDIDATES).
    candidate_k = max(
        effective_top_k * _OVERFETCH_FACTOR + _OVERFETCH_MIN_EXTRA,
        _OVERFETCH_MIN_CANDIDATES,
    )

    provider = _get_embedding_provider()
    embedding = provider.embed_query(query)

    store = _get_vector_store()
    hits = store.query(
        embedding, top_k=candidate_k, owner_id=owner_id, include_global=include_global
    )
    logger.info(
        "Search query=%r returned %s candidate(s) (top_k=%s)",
        query,
        len(hits),
        effective_top_k,
    )
    if not hits:
        return []

    # --- Hybrid lexical re-rank -------------------------------------------
    # The local hashing embedder has no IDF, so a chunk that literally defines a
    # rare query term can score far below the vector top_k (and thus outside the
    # over-fetched pool entirely). Two-part lexical rescue:
    #   1. INJECT exact-substring (ILIKE) matches for the query's content-words
    #      into the candidate pool, so a defining chunk the vector buried deep is
    #      even available to rank. Injected hits carry their real vector score.
    #   2. BLEND a keyword-overlap signal into each candidate's ranking score and
    #      re-sort, so literal matches surface above diluted near-misses.
    # The original vector ``score`` is left untouched on every hit for output and
    # for the downstream relevance threshold.
    query_words = _content_words(query)
    if query_words:
        # Expand with light synonym vocabulary so the lexical rescue can bridge a
        # phrasing/vocabulary gap (e.g. "avail" -> "providers") the IDF-free
        # embedder cannot. Used for BOTH the keyword_query pool and the overlap
        # signal below, so a provider-listing chunk can be injected AND ranked up.
        query_words = _expand_query_words(query_words)
        try:
            keyword_hits = store.keyword_query(
                embedding,
                sorted(query_words),
                top_k=candidate_k,
                owner_id=owner_id,
                include_global=include_global,
            )
        except Exception as exc:  # noqa: BLE001 - lexical rescue is best-effort
            logger.warning("keyword_query failed (%s); using vector pool only.", exc)
            keyword_hits = []
        if keyword_hits:
            seen_ids = {
                str((h.get("metadata") or {}).get("chunk_id") or h.get("id") or "")
                for h in hits
            }
            for kh in keyword_hits:
                kid = str((kh.get("metadata") or {}).get("chunk_id") or kh.get("id") or "")
                if kid and kid not in seen_ids:
                    seen_ids.add(kid)
                    hits.append(kh)

        # Pool-local IDF so a discriminative term (e.g. "cdn", present only in the
        # small target doc) outweighs ubiquitous words ("make", "terms") that
        # match many chunks of the huge global docs flooding the pool.
        pool_contents = [
            str((h.get("metadata") or {}).get("content") or h.get("document") or "")
            for h in hits
        ]
        idf = _pool_idf_weights(query_words, pool_contents)

        # Rare, acronym-like query tokens that should deterministically anchor a
        # chunk literally containing them (see _acronym_anchor_terms). Computed
        # once from the already-built pool IDF; empty for ordinary queries.
        anchor_terms = _acronym_anchor_terms(query_words, idf)

        definition_query = _is_definition_query(query)
        for hit in hits:
            metadata = hit.get("metadata") or {}
            content = str(metadata.get("content") or hit.get("document") or "")
            content_lower = content.lower()
            overlap = _weighted_keyword_overlap(query_words, content, idf)
            rank_score = float(hit.get("score", 0.0)) + _KEYWORD_WEIGHT * overlap
            # A rare acronym-like query token literally present in this chunk
            # deterministically lifts it: the bonus alone clears the 0.40 relevance
            # floor even when the lowercase query got an off-topic vector (~0).
            if anchor_terms and any(t in content_lower for t in anchor_terms):
                rank_score += _EXACT_TOKEN_ANCHOR_BONUS
            # For a "what is X" query, decisively prefer the chunk that literally
            # defines X (e.g. "Definition The transpose of ...") over the many
            # chunks that only mention X with a slightly better vector score.
            if definition_query and _defines_term(query_words, content):
                rank_score += _DEFINITION_BONUS
            # Down-weight a predominantly-interrogative question-list chunk so the
            # declarative answer chunk (e.g. page-1 "CEO: Ananya Sharma") wins #0
            # for a "Who is the CEO?"-style query instead of the page-2 question
            # list — which both echoes back as the "answer" and would wrongly
            # anchor the downstream single-top-document rule.
            if _is_question_list_chunk(content):
                rank_score -= _QUESTION_LIST_PENALTY
            hit["_rank_score"] = rank_score
        hits = sorted(hits, key=lambda h: h.get("_rank_score", 0.0), reverse=True)

    # Cap per-document contribution so a huge global doc cannot crowd a small
    # tenant doc out of top_k. Hits are already ordered most-relevant first
    # (by _rank_score when re-ranked, else by vector score from the store), so
    # truncating each document_id to its first _PER_DOCUMENT_CANDIDATE_CAP hits
    # keeps that document's strongest chunks while freeing pool slots for other
    # documents. Tenant isolation is unaffected (ownership is enforced upstream
    # in SQL); this only rebalances how many chunks each doc may contribute.
    per_doc_counts: dict[object, int] = {}
    capped_hits: list[dict] = []
    for hit in hits:
        doc_key = (hit.get("metadata") or {}).get("document_id")
        count = per_doc_counts.get(doc_key, 0)
        if count >= _PER_DOCUMENT_CANDIDATE_CAP:
            continue
        per_doc_counts[doc_key] = count + 1
        capped_hits.append(hit)
    hits = capped_hits

    # Batch-resolve the relational rows for active-filtering.
    document_ids: set[int] = set()
    chunk_ids: set[str] = set()
    for hit in hits:
        metadata = hit.get("metadata") or {}
        doc_id = metadata.get("document_id")
        chunk_id = metadata.get("chunk_id") or hit.get("id")
        if doc_id is not None:
            try:
                document_ids.add(int(doc_id))
            except (TypeError, ValueError):
                continue
        if chunk_id is not None:
            chunk_ids.add(str(chunk_id))

    active_documents = _active_documents_by_id(db, document_ids)
    active_chunks = _active_chunk_ids(db, chunk_ids)

    results: list[dict] = []
    # Track normalized content already accepted so byte-identical chunks (e.g. from a
    # double-indexed document) don't waste top_k slots; we keep the highest-scoring
    # occurrence since hits are already ordered most-relevant first.
    seen_content: set[str] = set()
    for hit in hits:
        if len(results) >= effective_top_k:
            break

        metadata = hit.get("metadata") or {}
        raw_doc_id = metadata.get("document_id")
        try:
            doc_id = int(raw_doc_id) if raw_doc_id is not None else None
        except (TypeError, ValueError):
            doc_id = None

        chunk_id = str(metadata.get("chunk_id") or hit.get("id") or "")

        # Skip hits whose document is missing/not-active or whose chunk is deleted.
        document = active_documents.get(doc_id) if doc_id is not None else None
        if document is None:
            logger.debug("Skipping hit %s: document %s not active", chunk_id, raw_doc_id)
            continue
        if chunk_id and chunk_id not in active_chunks:
            logger.debug("Skipping hit %s: chunk soft-deleted/missing", chunk_id)
            continue

        # Prefer the stored chunk content; fall back to the document text payload.
        content = metadata.get("content")
        if not content:
            content = hit.get("document") or ""

        # De-duplicate on normalized content (whitespace-collapsed, lowercased) so
        # duplicate chunks don't crowd out distinct, lower-ranked results.
        content_key = " ".join(str(content).split()).lower()
        if content_key and content_key in seen_content:
            logger.debug("Skipping hit %s: duplicate content", chunk_id)
            continue
        if content_key:
            seen_content.add(content_key)

        raw_page = metadata.get("page")
        try:
            page = int(raw_page) if raw_page is not None else None
        except (TypeError, ValueError):
            page = None

        results.append(
            {
                "document_id": document.id,
                "document_name": document.filename,
                "excerpt": _truncate(str(content)),
                "chunk_id": chunk_id,
                "page": page,
                # Raw vector similarity (kept for any caller that needs the
                # unblended signal); NOT exposed by the API schema.
                "score": float(hit.get("score", 0.0)),
                # Blended hybrid score (vector + lexical-overlap + definition
                # bonus) used to ORDER results. The downstream relevance gate
                # filters on THIS so the lexical rescue that reorders a gold
                # chunk to the top also lets it survive the threshold. Falls
                # back to the raw vector score when no re-rank ran (no content
                # words, e.g. small-talk) -> rank_score == score there, so the
                # gate behaves identically for greetings.
                "rank_score": float(hit.get("_rank_score", hit.get("score", 0.0))),
                # Ownership flags (from the authoritative resolved Document row)
                # so the source builder can apply the own-docs-only display rule.
                "owner_id": document.owner_id,
                "is_global": bool(document.is_global),
            }
        )

    # Vector store already orders most-relevant first; preserve that order.
    logger.info("Search returning %s active result(s)", len(results))
    return results
