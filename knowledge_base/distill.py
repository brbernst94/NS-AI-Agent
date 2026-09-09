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
INTERACTIONS_PER_MODULE = int(os.getenv("DISTILL_INTERACTIONS_PER_MODULE", "12"))
CHUNKS_PER_TOPIC = int(os.getenv("DISTILL_CHUNKS_PER_TOPIC", "14"))
# An interaction spans two features, so it needs passages about both.
CHUNKS_PER_QUERY = int(os.getenv("DISTILL_CHUNKS_PER_QUERY", "8"))
GUIDE_SOURCE = "distilled_guide"

_TOPIC_PROMPT = """You are cataloguing a NetSuite documentation corpus.

Below are real page titles from the "{module}" area of NetSuite's official documentation.

{titles}

Propose {n} distinct topics that this material covers and that a NetSuite administrator or consultant would actually need explained. Each topic should be specific enough to write a focused guide about — "Setting up multi-book accounting", not "Accounting".

Return JSON only, no prose: {{"topics": ["...", "..."]}}"""

_INTERACTION_PROMPT = """You are planning the knowledge a senior NetSuite consultant carries that the documentation never states in one place.

Oracle's documentation describes features one at a time. Consultants get asked about the seams between them: what one setting forbids elsewhere, what a feature quietly changes once enabled, why two things can't coexist on the same record. That knowledge is spread across many pages and stated as a rule on none of them.

Here is what the "{module}" material in this corpus covers:

{titles}

Propose {n} questions of that kind about this area. Each must be:
- a real question an administrator or consultant would be asked on a project
- about a consequence, constraint, interaction or trade-off — NOT "what is X" or "how do I set up X"
- answerable from NetSuite documentation, if someone read across enough of it
- specific to actual NetSuite features, never generic

Good shape: "Why can't items set to direct revenue posting share a sales order with items that have revenue recognition rules?", "What does enabling Multiple Shipping Routes change about how a sales order is fulfilled and billed?", "Which item record settings make an item unusable on a SuiteBilling subscription?"

Bad shape: "What is a sales order?", "How do I create a subsidiary?", "What are best practices for inventory?"

For each question also give 2-3 search queries that would retrieve the source material. Cover BOTH sides of the interaction — one query per feature involved, phrased as the documentation would phrase it, not as the question is phrased.

Return JSON only, no prose:
{{"interactions": [{{"question": "...", "queries": ["...", "..."]}}]}}"""

_INTERACTION_GUIDE_PROMPT = """You are a NetSuite systems expert answering this, for a consultant who needs to act on the answer: **{topic}**

Below are passages from NetSuite's official documentation, retrieved from across the corpus.

Rules:
- Lead with the direct answer — the rule, the constraint, the consequence. No preamble.
- Then why it works that way, in terms of what NetSuite is actually doing underneath.
- Then what it means in practice: what breaks, what the error looks like, what the workaround or correct sequence is.
- Be specific: exact field names and internal IDs, exact feature names, exact menu paths (Setup > Company > ...), exact limits. These are what make it useful.
- The passages may only partly cover this. Say plainly which part of the answer the documentation here does not establish, rather than filling the gap with something plausible. Never guess a field ID, feature name, menu path or limit.
- If the premise of the question is wrong, say so and explain what is actually true.
- 300-700 words. Dense. No filler.

PASSAGES:
{passages}"""

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
            "topics_proposed": 0, "interactions_proposed": 0, "guides_written": 0,
            "chunks_added": 0, "skipped": 0, "errors": [],
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
    def _save_topics(
        module: str | None,
        topics: list[str],
        kind: str = "topic",
        queries: dict[str, list[str]] | None = None,
    ) -> int:
        rows = [(module, t.strip()) for t in topics if isinstance(t, str) and t.strip()]
        if not rows:
            return 0
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            n = 0
            for module_, topic in rows:
                cur.execute(
                    """
                    INSERT INTO kb_topics (module, topic, status, kind, queries)
                    VALUES (%s, %s, 'pending', %s, %s)
                    ON CONFLICT (topic) DO NOTHING
                    """,
                    (module_, topic, kind, (queries or {}).get(topic) or None),
                )
                n += cur.rowcount
        return n

    def propose_interactions(self, per_module: int = INTERACTIONS_PER_MODULE) -> int:
        """Ask for the cross-feature questions the docs never answer in one place.

        Feature topics mirror how Oracle organised its pages. The questions
        consultants actually field sit between features and are a page title
        nowhere, so they need proposing separately.
        """
        added = 0
        for module in self.modules():
            if self._stop:
                break
            titles = self._sample_titles(module)
            if len(titles) < 5:
                continue
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=2000,
                    messages=[{"role": "user", "content": _INTERACTION_PROMPT.format(
                        module=module, n=per_module,
                        titles="\n".join(f"- {t[:110]}" for t in titles[:120]))}],
                )
                text = resp.content[0].text
                items = json.loads(text[text.index("{"): text.rindex("}") + 1])["interactions"]
            except Exception as exc:
                self.report["errors"].append(f"interactions/{module}: {exc}")
                logger.warning("Interaction proposal failed for %s: %s", module, exc)
                continue

            questions, queries = [], {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("question", "")).strip()
                if not q:
                    continue
                questions.append(q)
                subs = [str(s).strip() for s in item.get("queries", []) if str(s).strip()]
                queries[q] = subs[:3] or [q]
            added += self._save_topics(module, questions, kind="interaction", queries=queries)

        self.report["interactions_proposed"] = added
        logger.info("Proposed %d new interaction questions", added)
        return added

    # ------------------------------------------------------------------
    # Phase 2: write a guide per topic
    # ------------------------------------------------------------------

    def pending_topics(self, limit: int = 1000) -> list[tuple[int, str | None, str, str, list[str] | None]]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, module, topic, kind, queries FROM kb_topics
                WHERE status = 'pending'
                -- Interactions first. They are the material the raw corpus
                -- answers worst, and a run cut short by a restart would
                -- otherwise spend itself entirely on feature topics.
                ORDER BY (kind = 'interaction') DESC, id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()

    @staticmethod
    def _count_kind(kind: str) -> int:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM kb_topics WHERE kind = %s", (kind,))
            return cur.fetchone()[0]

    def _mark(self, topic_id: int, status: str, chunks: int = 0, error: str | None = None) -> None:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE kb_topics SET status=%s, guide_chunks=%s, error=%s, updated_at=NOW() WHERE id=%s",
                (status, chunks, error, topic_id),
            )

    def _retrieve(self, topic: str, queries: list[str] | None) -> list[dict[str, Any]]:
        """Passages for one topic, searched across the whole corpus.

        Over half the crawled pages are untagged, so this never filters by
        module. An interaction searches once per stored query and merges the
        results, because a single search on the question tends to return only
        the feature it happens to name first.
        """
        searches = [(q, CHUNKS_PER_QUERY) for q in (queries or [])] or [(topic, CHUNKS_PER_TOPIC)]
        merged: dict[str, dict[str, Any]] = {}
        for query, n in searches:
            for hit in self.index.search(query, n_results=n):
                if hit.get("source") == GUIDE_SOURCE:
                    continue
                key = hit["text"][:200]
                if key not in merged or hit.get("score", 0) > merged[key].get("score", 0):
                    merged[key] = hit
        return sorted(merged.values(), key=lambda h: h.get("score", 0), reverse=True)

    def write_guide(
        self,
        topic_id: int,
        module: str | None,
        topic: str,
        kind: str = "topic",
        queries: list[str] | None = None,
    ) -> int:
        hits = self._retrieve(topic, queries if kind == "interaction" else None)
        if len(hits) < 3:
            self._mark(topic_id, "skipped", error="too few source passages")
            self.report["skipped"] += 1
            return 0

        passages = "\n\n---\n\n".join(
            f"[{(h.get('metadata') or {}).get('title') or h['source']}]\n{h['text'][:2200]}"
            for h in hits
        )
        template = _INTERACTION_GUIDE_PROMPT if kind == "interaction" else _GUIDE_PROMPT
        resp = self.client.messages.create(
            model=self.model, max_tokens=2600,
            messages=[{"role": "user", "content": template.format(topic=topic, passages=passages)}],
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
        # Propose per kind, not "only when nothing is pending": on a deployment
        # whose feature topics are already written, interactions would otherwise
        # never get proposed at all.
        if not self._count_kind("topic"):
            self.propose_topics()
        if not self._count_kind("interaction"):
            self.propose_interactions()

        todo = self.pending_topics(max_topics or 1000)
        logger.info("Distilling %d topics with %s", len(todo), self.model)
        for topic_id, module, topic, kind, queries in todo:
            if self._stop:
                break
            self.last_topic = topic
            try:
                n = self.write_guide(topic_id, module, topic, kind=kind, queries=queries)
                logger.info("Guide written (%s): %s (%d chunks)", kind, topic, n)
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
            cur.execute("SELECT kind, COUNT(*) FROM kb_topics WHERE status='done' GROUP BY kind")
            done_by_kind = {r[0]: r[1] for r in cur.fetchall()}
        return {
            "pages_crawled": self.report["guides_written"],  # for the shared runner
            "guides_written": self.report["guides_written"],
            "done_by_kind": done_by_kind,
            "chunks_added": self.report["chunks_added"],
            "queued": by_status.get("pending", 0),
            "by_status": by_status,
            "last_url": self.last_topic,
            "errors": len(self.report["errors"]),
        }

    def crawl(self, max_records: int | None = None) -> int:
        """Entry point name the crawl manager uses."""
        return self.run(max_topics=max_records)


def list_topics(
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The proposed topics themselves, so their quality can be judged.

    Counts say a run happened; only reading the questions says whether the
    proposals are worth writing guides for.
    """
    clauses, params = [], []
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, kind, module, topic, status, queries, guide_chunks, error
            FROM kb_topics {where} ORDER BY id LIMIT %s
            """,
            (*params, max(1, min(limit, 500))),
        )
        return [
            {
                "id": r[0], "kind": r[1], "module": r[2], "topic": r[3],
                "status": r[4], "queries": r[5], "guide_chunks": r[6], "error": r[7],
            }
            for r in cur.fetchall()
        ]


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
        cur.execute("SELECT kind, status, COUNT(*) FROM kb_topics GROUP BY kind, status")
        by_kind: dict[str, dict[str, int]] = {}
        for kind, status, count in cur.fetchall():
            by_kind.setdefault(kind, {})[status] = count
    return {
        "by_status": by_status,
        "by_kind": by_kind,
        "guide_chunks": chunks,
        "guides_by_module": by_module,
    }
