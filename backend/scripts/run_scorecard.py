"""Run the 41-question multilingual scorecard through chat_service.query.

Scores 6 metrics per question and prints a JSON summary. Owner is the eval user
(id 2) so the private RAG_Test_Dataset.pdf + global docs are both candidates.

Run from backend/:  ./venv/bin/python scripts/run_scorecard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import SessionLocal  # noqa: E402
from app.services import chat_service, search_service  # noqa: E402

EVAL_OWNER_ID = 2
QUESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".a5c", "processes", "rag-eval-questions.json",
)

# Per-question keyword expectations for the correctAnswer metric. The grader is
# substring/keyword based (the live answers are free text). For each id we list
# the must-contain tokens (ALL required) plus, for negatives, the must-ABSTAIN
# check is handled separately.
GOLD = {
    "en-direct-1": ["ananya sharma"],
    "en-direct-2": ["5000"],
    "en-semantic-1": ["ananya sharma"],
    "en-semantic-2": ["5000"],
    "en-indirect-1": ["3", "remot"],
    "en-indirect-2": [["hybrid", "no", "not"]],  # any-of group
    "en-multihop-1": ["26", "15"],
    "en-multihop-2": ["10 lakh", "50,000"],
    "en-comparison-1": ["24", "12"],
    "en-comparison-2": ["maternity", "26"],
    "en-summary-1": ["24", "12", "26"],
    "en-summary-2": ["5000", "1500"],
    # Proper-noun "Sharma" may be kept latinized (eval allows original proper
    # nouns), so accept either the native-script or Latin spelling.
    "hi-direct-1": [["शर्मा", "sharma"]],
    "hi-direct-2": ["5000"],
    "hi-semantic-1": [["शर्मा", "sharma"]],
    "hi-semantic-2": ["5000"],
    "hi-indirect-1": ["3", ["नहीं", "घर", "दूर"]],
    "hi-indirect-2": ["3"],
    "bn-direct-1": [["শর্মা", "sharma"]],
    "bn-direct-2": ["5000"],
    "bn-semantic-1": [["শর্মা", "sharma"]],
    "bn-semantic-2": ["5000"],
    "bn-indirect-1": ["3", ["না", "বাড়ি", "দূর"]],
    "bn-indirect-2": ["3"],
    "cross-lang-1": ["450"],
    "cross-lang-2": ["26"],
    "cross-lang-3": ["10 lakh", "10 लाख"],  # any-of via group below
    "pronoun-1": ["ananya sharma"],
    "pronoun-2": ["bengaluru"],
    "acronym-1": [["yes", "mandatory", "required"], "mfa"],
    "acronym-2": [["yes", "mandatory", "required"]],
    "adversarial-1": [["no", "3", "not"]],
    "adversarial-2": [["no", "hybrid", "3"]],
    "reasoning-1": ["economy", "5000"],
    "reasoning-2": ["50,000"],
    "en-direct-3": ["90"],
    "en-direct-4": ["24"],
}
# cross-lang-3 accepts either english or hindi lakh form
GOLD["cross-lang-3"] = [["10 lakh", "10 लाख", "₹10"]]

NEGATIVES = {"negative-1", "negative-2", "negative-3", "negative-4"}

ABSTAIN_MARKERS = [
    "not available", "could not find", "couldn't find", "couldn t find",
    "no relevant information", "not in the knowledge base", "not find",
    "नहीं मिल", "उपलब्ध नहीं", "पाई नहीं", "পাওয়া যায়নি", "নেই",
    "don't have", "do not have", "not mentioned", "not specified", "not provide",
    "unable to find", "no information",
]

# Source sentence keyword(s) that identify the gold chunk for retrieval scoring.
GOLD_CHUNK_KW = {
    "ceo": ["ceo: ananya", "ananya sharma"],
    "hotel": ["hotel reimbursement"],
    "remote": ["remotely up to 3"],
    "leave": ["annual leave: 24", "maternity leave"],
    "benefits": ["health insurance", "learning budget"],
    "security": ["passwords must be changed", "multi-factor"],
    "head": ["head office: bengaluru"],
    "employees": ["employees: 450"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _has_all(answer: str, reqs) -> bool:
    a = _norm(answer)
    for r in reqs:
        if isinstance(r, list):
            if not any(_norm(x) in a for x in r):
                return False
        else:
            if _norm(r) not in a:
                return False
    return True


# Signatures of the LocalExtractiveLLMProvider fallback (raw chunk fragments that
# Gemini would never emit) — used to detect a transient 429/timeout fallback and
# retry the question on the real LLM.
_FALLBACK_MARKERS = [
    "operations? 4. how many people",
    "■■■",
    "y leave: 26 weeks paternity",
    "what benefits support employee wellbeing and learning? does the company",
    "multi-factor authentication is mandatory. employees must report security incidents within 24 hours. benefits",
    "office timing: 9:30 am to 6:30 pm unused annual leave",
    "what is linear algebra?",
]


def _looks_like_fallback(answer: str) -> bool:
    a = _norm(answer)
    return any(m in a for m in _FALLBACK_MARKERS)


def _is_abstain(answer: str) -> bool:
    a = _norm(answer)
    return any(_norm(m) in a for m in ABSTAIN_MARKERS)


def _detect_script(text: str) -> str:
    for ch in text:
        if "ऀ" <= ch <= "ॿ":
            return "hi"
        if "ঀ" <= ch <= "৿":
            return "bn"
    return "en"


def _lang_preserved(qlang: str, answer: str) -> bool:
    # Strip latinized glosses in parentheses for the script check; require the
    # answer to contain script of the question language for hi/bn.
    if qlang == "en":
        # English answers should be predominantly latin (allow proper nouns).
        return _detect_script(answer) == "en" or "ananya" in _norm(answer)
    target_range = ("ऀ", "ॿ") if qlang == "hi" else ("ঀ", "৿")
    return any(target_range[0] <= ch <= target_range[1] for ch in answer)


def main() -> None:
    search_service.reset_caches()
    data = json.load(open(QUESTIONS_PATH))
    questions = data["questions"]
    results = []
    db = SessionLocal()
    try:
        for q in questions:
            qid = q["id"]
            # Retry on a detected local-provider fallback (Gemini 429/timeout): the
            # local extractive provider echoes raw chunk fragments verbatim, which
            # is not a real generation. We re-issue after a pause so a transient
            # free-tier throttle doesn't pollute the score with fallback noise.
            answer = ""
            sources = []
            for attempt in range(4):
                res = chat_service.query(db, question=q["question"], owner_id=EVAL_OWNER_ID)
                answer = res.get("answer", "")
                sources = res.get("sources", [])
                if not _looks_like_fallback(answer):
                    break
                time.sleep(20.0)
            # Retrieve hits directly to score retrieval metrics.
            hits = search_service.search(db, query=q["question"], owner_id=EVAL_OWNER_ID)
            top3_docs = hits[:3]
            top1 = hits[0] if hits else None

            is_neg = qid in NEGATIVES
            abstain = _is_abstain(answer)
            if is_neg:
                correct = abstain
            else:
                reqs = GOLD.get(qid, [])
                correct = _has_all(answer, reqs) and not abstain

            # Retrieved correct chunk: top1 / top3 from the test PDF (doc 49) for
            # in-pdf questions; for negatives there's no gold chunk.
            retrieved_top1 = bool(top1 and top1["document_id"] == 49) if not is_neg else None
            retrieved_top3 = (
                any(h["document_id"] == 49 for h in top3_docs) if not is_neg else None
            )

            hallucination_free = abstain if is_neg else (correct or abstain)
            lang_ok = _lang_preserved(q["language"], answer)
            citation = bool(sources) if (not is_neg and q.get("answerInPdf")) else True

            results.append({
                "id": qid, "lang": q["language"], "category": q["category"],
                "correctAnswer": correct,
                "retrievedCorrectChunk": retrieved_top1,
                "retrievedTop3": retrieved_top3,
                "hallucinationFree": hallucination_free,
                "languagePreserved": lang_ok,
                "citationIncluded": citation,
                "abstain": abstain,
                "answer": answer[:160].replace("\n", " "),
            })
            mark = "OK " if correct else "XX "
            print(f"{mark}{qid:16s} [{q['language']}] {answer[:90].strip()!r}", flush=True)
            # Pace under the Gemini free-tier per-minute cap; the per-question
            # fallback-retry loop above absorbs any transient throttle.
            time.sleep(6.0)
    finally:
        db.close()

    n = len(results)
    def rate(key, subset=None):
        rs = [r for r in results if subset is None or subset(r)]
        vals = [r[key] for r in rs if r[key] is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    local_pass = rate("correctAnswer")
    direct_ids = [r for r in results if r["id"].startswith("en-direct")]
    direct_ok = all(r["correctAnswer"] for r in direct_ids)
    ceo = next(r for r in results if r["id"] == "en-direct-1")
    multi = rate("correctAnswer", lambda r: r["lang"] in ("hi", "bn") or r["id"].startswith("cross"))
    negs = [r for r in results if r["id"] in NEGATIVES]
    negs_ok = all(r["correctAnswer"] for r in negs)

    summary = {
        "n": n,
        "localPassRate": local_pass,
        "english": rate("correctAnswer", lambda r: r["lang"] == "en"),
        "hindi": rate("correctAnswer", lambda r: r["lang"] == "hi"),
        "bengali": rate("correctAnswer", lambda r: r["lang"] == "bn"),
        "multilingualPassRate": multi,
        "directQuestionsAnswered": direct_ok,
        "ceoAnswer": ceo["answer"],
        "negativesCorrect": negs_ok,
        "retrievedTop1": rate("retrievedCorrectChunk"),
        "retrievedTop3": rate("retrievedTop3"),
        "hallucinationFree": rate("hallucinationFree"),
        "languagePreserved": rate("languagePreserved"),
        "citationIncluded": rate("citationIncluded"),
    }
    print("\n===SUMMARY===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    json.dump({"summary": summary, "results": results},
              open("/tmp/scorecard_result.json", "w"), ensure_ascii=False, indent=2)
    # Print failures for inspection
    print("\n===FAILURES===")
    for r in results:
        if not r["correctAnswer"]:
            print(r["id"], "|", r["answer"])


if __name__ == "__main__":
    main()
