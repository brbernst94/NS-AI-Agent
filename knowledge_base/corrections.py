"""Practitioner corrections: what a NetSuite expert taught the agent.

Oracle's documentation describes mechanisms. Practitioners know which mechanism
is actually the right one to reach for, and that knowledge is on no page
anywhere. When the agent answers wrongly and an expert corrects it, that
correction is the most valuable thing in the knowledge base — it is the only
content here that did not come from a document.

Corrections are stored twice on purpose:
  - in `kb_corrections`, structured, so they survive a rebuild of the knowledge
    base, can be reviewed, and can be exported as an eval answer key;
  - as a `doc_type='correction'` chunk, so retrieval actually surfaces them.
    `index.py` gives that doc_type a boost well clear of distilled guides,
    because one correction competes against thousands of chunks that disagree.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from knowledge_base import db

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "practitioner_correction"
MODEL = os.getenv("CORRECTION_MODEL", os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
# How many existing chunks to examine for contradictions when a correction lands.
SUPERSEDE_CANDIDATES = int(os.getenv("CORRECTION_SUPERSEDE_CANDIDATES", "12"))

_TOPIC_PROMPT = """A NetSuite practitioner has corrected an AI assistant that answered wrongly.

THE QUESTION ASKED: {question}

THE WRONG ANSWER THE ASSISTANT GAVE: {wrong_answer}

WHAT THE PRACTITIONER SAYS IS ACTUALLY TRUE: {correction}

Rewrite what the practitioner said as a short, authoritative reference passage about the underlying rule — the way correct documentation would state it.

Rules:
- State the rule directly as fact. Never mention the assistant, the wrong answer, a correction, or that anyone was mistaken. The passage must read as settled knowledge, not as a dispute.
- Generalise to the topic, not the exact question. Someone asking the same thing in different words must be served by this passage.
- Keep every specific the practitioner gave: tab names, statuses, field names, the sequence of steps, when it does and does not apply.
- Where the practitioner explained *when* to use one approach over another, keep that judgement — it is the most valuable part.
- Add nothing they did not say. No invented field IDs, menu paths or limits.
- 80-200 words. No preamble, no headings.

Then give 4-6 short search phrasings someone might use to reach this knowledge — different wordings of the same question, as a user would type them.

Return JSON only: {{"passage": "...", "phrasings": ["...", "..."]}}"""

_CONTRADICTION_PROMPT = """A NetSuite practitioner has established this as true:

{passage}

Below are passages currently in a knowledge base. Identify which ones state something that DIRECTLY CONTRADICTS the practitioner's statement above — passages a reader would come away from believing the opposite.

Only flag a direct contradiction on this specific point. Do NOT flag a passage merely because it covers the same topic, is adjacent, or is incomplete.

{candidates}

Return JSON only, listing the numbers of contradicting passages: {{"contradicting": [1, 4]}}"""


def _client() -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _as_topic_knowledge(question: str, correction: str, wrong_answer: str | None) -> tuple[str, list[str]]:
    """Rewrite a correction as standalone topic knowledge.

    A correction stored as "the assistant said X, but actually Y" retrieves
    fine and still loses: the model reads the wrong answer restated at length
    and treats the whole thing as one disputed opinion against a corpus that
    agrees with its own prior. Stated flatly as fact, with no mention that
    anyone was corrected, it reads as knowledge and wins.
    """
    try:
        resp = _client().messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": _TOPIC_PROMPT.format(
                question=question,
                wrong_answer=wrong_answer or "(not recorded)",
                correction=correction)}],
        )
        import json as _json

        text = resp.content[0].text
        data = _json.loads(text[text.index("{"): text.rindex("}") + 1])
        passage = (data.get("passage") or "").strip()
        phrasings = [str(p).strip() for p in data.get("phrasings", []) if str(p).strip()]
        if passage:
            return passage, phrasings[:6]
    except Exception as exc:
        logger.warning("Could not rewrite correction as topic knowledge: %s", exc)
    # Fall back to the practitioner's own words rather than losing the lesson.
    return correction, []


def _build_body(passage: str, phrasings: list[str]) -> str:
    """The text that gets embedded and retrieved for a correction."""
    parts = [passage, ""]
    if phrasings:
        # Embedded so the chunk matches however the question gets asked.
        parts += ["Also asked as: " + "; ".join(phrasings), ""]
    parts.append(
        "Established by a NetSuite practitioner working in a live account. "
        "This is authoritative and overrides any documentation or guide that disagrees."
    )
    return "\n".join(parts)


def supersede_contradictions(
    knowledge_index: Any,
    passage: str,
    queries: list[str],
    correction_id: int,
) -> int:
    """Retire chunks that directly contradict a correction.

    Outranking is not enough. The contradicting material stays in the corpus,
    keeps getting retrieved alongside the correction, and the model sides with
    the majority. Marking it 'superseded' removes it from retrieval entirely.
    """
    seen: dict[str, dict[str, Any]] = {}
    for q in queries:
        for hit in knowledge_index.search(q, n_results=SUPERSEDE_CANDIDATES):
            meta = hit.get("metadata") or {}
            if meta.get("doc_type") in ("correction", "superseded"):
                continue
            seen.setdefault(hit["text"][:200], hit)
    candidates = list(seen.values())[:SUPERSEDE_CANDIDATES]
    if not candidates:
        return 0

    listing = "\n\n".join(
        f"[{i}] {h['text'][:1200]}" for i, h in enumerate(candidates, 1)
    )
    try:
        resp = _client().messages.create(
            model=MODEL, max_tokens=400,
            messages=[{"role": "user", "content": _CONTRADICTION_PROMPT.format(
                passage=passage, candidates=listing)}],
        )
        import json as _json

        text = resp.content[0].text
        nums = _json.loads(text[text.index("{"): text.rindex("}") + 1]).get("contradicting", [])
    except Exception as exc:
        logger.warning("Contradiction check failed for correction %s: %s", correction_id, exc)
        return 0

    retired = 0
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        for n in nums:
            try:
                hit = candidates[int(n) - 1]
            except (ValueError, IndexError, TypeError):
                continue
            cur.execute(
                """
                UPDATE knowledge_documents
                SET doc_type = 'superseded',
                    metadata = COALESCE(metadata, '{}'::jsonb)
                               || jsonb_build_object('superseded_by_correction', %s)
                WHERE left(text, 200) = %s AND doc_type IS DISTINCT FROM 'superseded'
                """,
                (correction_id, hit["text"][:200]),
            )
            retired += cur.rowcount
    if retired:
        logger.info("Correction %s retired %d contradicting chunks", correction_id, retired)
    return retired


def find_duplicate(question: str, correction: str) -> int | None:
    """An existing correction with the same question and text, if any."""
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM kb_corrections
            WHERE lower(btrim(question)) = lower(btrim(%s))
              AND lower(btrim(correction)) = lower(btrim(%s))
            ORDER BY id LIMIT 1
            """,
            (question, correction),
        )
        row = cur.fetchone()
        return row[0] if row else None


def save_correction(
    knowledge_index: Any,
    question: str,
    correction: str,
    wrong_answer: str | None = None,
    module: str | None = None,
    author: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record a correction, make it retrievable, and retire what it contradicts."""
    question = (question or "").strip()
    correction = (correction or "").strip()
    if not question or not correction:
        raise ValueError("both a question and a correction are required")

    # Streamlit re-runs a button handler on every rerun, so the same correction
    # arrives several times. Return the original instead of stacking copies.
    existing = find_duplicate(question, correction)
    if existing is not None:
        rows = [c for c in list_corrections(limit=500) if c["id"] == existing]
        if rows:
            return {**rows[0], "duplicate_of": existing}

    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb_corrections
                (question, wrong_answer, correction, module, author, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (question, wrong_answer, correction, module, author, session_id),
        )
        correction_id, created_at = cur.fetchone()

    passage, phrasings = _as_topic_knowledge(question, correction, wrong_answer)

    from knowledge_base.ingest import DocumentIngester

    chunks = DocumentIngester(knowledge_index).ingest_text(
        _build_body(passage, phrasings),
        source_name=f"{SOURCE_PREFIX}_{correction_id}",
        tags={
            "doc_type": "correction",
            "module": module,
            "title": question[:200],
            "system": "netsuite",
        },
    )

    retired = supersede_contradictions(
        knowledge_index, passage, [question, *phrasings], correction_id
    )

    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE kb_corrections SET chunks_added = %s, retired_chunks = %s WHERE id = %s",
            (chunks, retired, correction_id),
        )

    logger.info(
        "Correction %s saved (%d chunks, %d retired): %s",
        correction_id, chunks, retired, question[:80],
    )
    return {
        "id": correction_id,
        "question": question,
        "correction": correction,
        "wrong_answer": wrong_answer,
        "module": module,
        "passage": passage,
        "phrasings": phrasings,
        "chunks_added": chunks,
        "retired_chunks": retired,
        "created_at": created_at.isoformat() if created_at else None,
    }


def list_corrections(limit: int = 100, module: str | None = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if module:
        clauses.append("module = %s")
        params.append(module)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, question, wrong_answer, correction, module, author,
                   chunks_added, COALESCE(retired_chunks, 0), created_at
            FROM kb_corrections {where} ORDER BY created_at DESC LIMIT %s
            """,
            (*params, max(1, min(limit, 500))),
        )
        return [
            {
                "id": r[0], "question": r[1], "wrong_answer": r[2], "correction": r[3],
                "module": r[4], "author": r[5], "chunks_added": r[6],
                "retired_chunks": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
            }
            for r in cur.fetchall()
        ]


def stats() -> dict[str, Any]:
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM kb_corrections")
        total = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COALESCE(module, 'untagged'), COUNT(*)
            FROM kb_corrections GROUP BY 1 ORDER BY 2 DESC
            """
        )
        by_module = {r[0]: r[1] for r in cur.fetchall()}
    return {"total": total, "by_module": by_module}
