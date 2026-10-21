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
from typing import Any

from knowledge_base import db

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "practitioner_correction"


def _as_document(question: str, correction: str, wrong_answer: str | None) -> str:
    """Render a correction as the text that gets embedded and retrieved.

    The question is included because retrieval matches on the user's phrasing,
    and the wrong answer because naming the mistake explicitly is what stops the
    model repeating it — a bare statement of the right answer competes with the
    documentation, while 'this is wrong, and here is why' overrides it.
    """
    parts = [f"# Practitioner correction: {question.strip()}", ""]
    if wrong_answer and wrong_answer.strip():
        parts += [
            "An earlier answer got this WRONG. The incorrect answer was:",
            "",
            wrong_answer.strip(),
            "",
        ]
    parts += [
        "The correct answer, from a NetSuite practitioner:",
        "",
        correction.strip(),
        "",
        "This correction comes from someone who does this work in a real NetSuite "
        "account. It overrides the official documentation and any distilled guide "
        "that disagrees with it.",
    ]
    return "\n".join(parts)


def save_correction(
    knowledge_index: Any,
    question: str,
    correction: str,
    wrong_answer: str | None = None,
    module: str | None = None,
    author: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record a correction and make it retrievable. Returns the stored row."""
    question = (question or "").strip()
    correction = (correction or "").strip()
    if not question or not correction:
        raise ValueError("both a question and a correction are required")

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

    from knowledge_base.ingest import DocumentIngester

    chunks = DocumentIngester(knowledge_index).ingest_text(
        _as_document(question, correction, wrong_answer),
        source_name=f"{SOURCE_PREFIX}_{correction_id}",
        tags={
            "doc_type": "correction",
            "module": module,
            "title": question[:200],
            "system": "netsuite",
        },
    )

    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE kb_corrections SET chunks_added = %s WHERE id = %s",
            (chunks, correction_id),
        )

    logger.info("Correction %s saved (%d chunks): %s", correction_id, chunks, question[:80])
    return {
        "id": correction_id,
        "question": question,
        "correction": correction,
        "wrong_answer": wrong_answer,
        "module": module,
        "chunks_added": chunks,
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
                   chunks_added, created_at
            FROM kb_corrections {where} ORDER BY created_at DESC LIMIT %s
            """,
            (*params, max(1, min(limit, 500))),
        )
        return [
            {
                "id": r[0], "question": r[1], "wrong_answer": r[2], "correction": r[3],
                "module": r[4], "author": r[5], "chunks_added": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
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
