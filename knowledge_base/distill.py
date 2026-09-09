"""Distil raw crawled fragments into authoritative topic guides.

The crawl leaves ~60k fragments of Oracle's pages. Fragments answer badly: a
question gets three partial passages that each half-cover it. This reads the
fragments topic by topic and writes one dense, correct guide per topic, stored
back as `doc_type='guide'` so retrieval prefers it over the raw material.

Two phases, both resumable:
  1. propose topics — sample real page titles and ask Claude what topics the
     corpus actually covers, so topics come from the content, not guesswork.
  2. write guides — for each topic, retrieve the best passages across the whole
     corpus (tagged or not) and synthesise a guide grounded only in them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

import anthropic

from knowledge_base import db

logger = logging.getLogger(__name__)

MODEL = os.getenv("DISTILL_MODEL", os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
TOPICS_PER_MODULE = int(os.getenv("DISTILL_TOPICS_PER_MODULE", "10"))
CHUNKS_PER_TOPIC = int(os.getenv("DISTILL_CHUNKS_PER_TOPIC", "14"))
GUIDE_SOURCE = "distilled_guide"

_TOPIC_PROMPT = """You are cataloguing a NetSuite documentation corpus.

Below are real page titles from the "{module}" area of NetSuite's official documentation.

{titles}

Propose {n} distinct topics that this material covers and that a NetSuite administrator or consultant would actually need explained. Each topic should be specific enough to write a focused guide about — "Setting up multi-book accounting", not "Accounting".

Return JSON only, no prose: {{"topics": ["...", "..."]}}"""

_GUIDE_PROMPT = """You are a NetSuite systems expert writing a reference guide on: **{topic}**

Below are passages from NetSuite's official documentation. Write a single authoritative guide on this topic, grounded ONLY in these passages.

Rules:
- Be specific and concrete: exact menu paths (Setup > Company > ...), exact field internal IDs, exact limits and formats. These are what make the guide useful.
- State prerequisites and the order things must be done in.
- Call out gotchas, limits, and things that commonly go wrong.
- If the passages don't cover something important about this topic, say so plainly rather than inventing it. Never guess a field ID, menu path or limit.
- No preamble, no "in this guide we will". Start with the substance.
- Aim for 400-900 words of dense, useful content.

PASSAGES:
{passages}"""


class Distiller:
    def __init__(self, knowledge_index: Any, model: str = MODEL) -> None:
        self.index = knowledge_index
        self.model = model
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.report: dict[str, Any] = {
            "topics_proposed": 0, "guides_written": 0, "chunks_added": 0,
            "skipped": 0, "errors": [],
        }
        self.last_topic: str | None = None
        self._stop = False

    # ------------------------------------------------------------------
    # Phase 1: propose topics from the corpus itself
    # ------------------------------------------------------------------

    def _sample_titles(self, module: str | None, limit: int = 120) -> list[str]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            if module:
                cur.execute(
                    """
                    SELECT DISTINCT title FROM knowledge_documents
                    WHERE module = %s AND title IS NOT NULL AND length(title) > 8
                    ORDER BY title LIMIT %s
                    """,
                    (module, limit),
                )
            else:
                # DISTINCT and ORDER BY random() can't coexist in one SELECT,
                # so dedupe in a subquery and sample from that.
                cur.execute(
                    """
                    SELECT title FROM (
                        SELECT DISTINCT title FROM knowledge_documents
                        WHERE module IS NULL AND title IS NOT NULL
                          AND length(title) > 8 AND doc_type = 'help'
                    ) t
                    ORDER BY random() LIMIT %s
                    """,
                    (limit,),
                )
            return [r[0] for r in cur.fetchall()]

    def modules(self) -> list[str]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT module, COUNT(*) FROM knowledge_documents
                WHERE module IS NOT NULL GROUP BY module ORDER BY 2 DESC
                """
            )
            return [r[0] for r in cur.fetchall()]

    def propose_topics(self, per_module: int = TOPICS_PER_MODULE) -> int:
        """Ask Claude what topics the corpus covers, module by module."""
        added = 0
        targets: list[str | None] = list(self.modules()) + [None]  # None = untagged pages
        for module in targets:
            if self._stop:
                break
            titles = self._sample_titles(module)
            if len(titles) < 5:
                continue
            label = module or "general"
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=1200,
                    messages=[{"role": "user", "content": _TOPIC_PROMPT.format(
                        module=label, n=per_module,
                        titles="\n".join(f"- {t[:110]}" for t in titles[:120]))}],
                )
                text = resp.content[0].text
                topics = json.loads(text[text.index("{"): text.rindex("}") + 1])["topics"]
            except Exception as exc:
                self.report["errors"].append(f"topics/{label}: {exc}")
                logger.warning("Topic proposal failed for %s: %s", label, exc)
                continue
            added += self._save_topics(module, topics)
        self.report["topics_proposed"] = added
        logger.info("Proposed %d new topics", added)
        return added

    @staticmethod
    def _save_topics(module: str | None, topics: list[str]) -> int:
        rows = [(module, t.strip()) for t in topics if isinstance(t, str) and t.strip()]
        if not rows:
            return 0
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            n = 0
            for module_, topic in rows:
                cur.execute(
                    """
                    INSERT INTO kb_topics (module, topic, status) VALUES (%s, %s, 'pending')
                    ON CONFLICT (topic) DO NOTHING
                    """,
                    (module_, topic),
                )
                n += cur.rowcount
        return n

    # ------------------------------------------------------------------
    # Phase 2: write a guide per topic
    # ------------------------------------------------------------------

    def pending_topics(self, limit: int = 1000) -> list[tuple[int, str | None, str]]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, module, topic FROM kb_topics WHERE status = 'pending' ORDER BY id LIMIT %s",
                (limit,),
            )
            return cur.fetchall()

    def _mark(self, topic_id: int, status: str, chunks: int = 0, error: str | None = None) -> None:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE kb_topics SET status=%s, guide_chunks=%s, error=%s, updated_at=NOW() WHERE id=%s",
                (status, chunks, error, topic_id),
            )

    def write_guide(self, topic_id: int, module: str | None, topic: str) -> int:
        # Retrieve across the whole corpus, not just the module: over half the
        # crawled pages are untagged, and excluding them would starve the guide.
        hits = self.index.search(topic, n_results=CHUNKS_PER_TOPIC)
        hits = [h for h in hits if h.get("source") != GUIDE_SOURCE]
        if len(hits) < 3:
            self._mark(topic_id, "skipped", error="too few source passages")
            self.report["skipped"] += 1
            return 0

        passages = "\n\n---\n\n".join(
            f"[{(h.get('metadata') or {}).get('title') or h['source']}]\n{h['text'][:2200]}"
            for h in hits
        )
        resp = self.client.messages.create(
            model=self.model, max_tokens=2600,
            messages=[{"role": "user", "content": _GUIDE_PROMPT.format(topic=topic, passages=passages)}],
        )
        guide = resp.content[0].text.strip()
        if len(guide) < 200:
            self._mark(topic_id, "failed", error="model returned too little")
            return 0

        from knowledge_base.ingest import DocumentIngester

        chunks = DocumentIngester(self.index).ingest_text(
            f"# {topic}\n\n{guide}",
            source_name=GUIDE_SOURCE,
            tags={"doc_type": "guide", "module": module, "title": topic, "system": "netsuite"},
        )
        self._mark(topic_id, "done", chunks=chunks)
        self.report["guides_written"] += 1
        self.report["chunks_added"] += chunks
        return chunks

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, max_topics: int | None = None, progress: Callable[[dict], None] | None = None) -> int:
        if not self.pending_topics(1):
            self.propose_topics()

        todo = self.pending_topics(max_topics or 1000)
        logger.info("Distilling %d topics with %s", len(todo), self.model)
        for topic_id, module, topic in todo:
            if self._stop:
                break
            self.last_topic = topic
            try:
                n = self.write_guide(topic_id, module, topic)
                logger.info("Guide written: %s (%d chunks)", topic, n)
            except Exception as exc:
                logger.warning("Guide failed for '%s': %s", topic, exc)
                self.report["errors"].append(f"{topic}: {exc}")
                self._mark(topic_id, "failed", error=str(exc)[:400])
            if progress:
                progress(self.status())
        return self.report["guides_written"]

    def stop(self) -> None:
        self._stop = True

    def status(self) -> dict[str, Any]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM kb_topics GROUP BY status")
            by_status = {r[0]: r[1] for r in cur.fetchall()}
        return {
            "pages_crawled": self.report["guides_written"],  # for the shared runner
            "guides_written": self.report["guides_written"],
            "chunks_added": self.report["chunks_added"],
            "queued": by_status.get("pending", 0),
            "by_status": by_status,
            "last_url": self.last_topic,
            "errors": len(self.report["errors"]),
        }

    def crawl(self, max_records: int | None = None) -> int:
        """Entry point name the crawl manager uses."""
        return self.run(max_topics=max_records)


def stats() -> dict[str, Any]:
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM kb_topics GROUP BY status")
        by_status = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COALESCE(SUM(guide_chunks),0) FROM kb_topics WHERE status='done'")
        chunks = cur.fetchone()[0]
        cur.execute(
            "SELECT module, COUNT(*) FROM kb_topics WHERE status='done' GROUP BY module ORDER BY 2 DESC"
        )
        by_module = {(r[0] or "general"): r[1] for r in cur.fetchall()}
    return {"by_status": by_status, "guide_chunks": chunks, "guides_by_module": by_module}
