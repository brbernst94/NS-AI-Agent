"""Persistent crawl bookkeeping in PostgreSQL (survives redeploys)."""

from __future__ import annotations

from typing import Any

from knowledge_base import db


class CrawlState:
    def __init__(self, crawler: str) -> None:
        self.crawler = crawler

    def is_done(self, url: str) -> bool:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM crawl_urls WHERE url = %s AND status IN ('done', 'skipped')", (url,)
            )
            return cur.fetchone() is not None

    def done_urls(self) -> set[str]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT url FROM crawl_urls WHERE crawler = %s AND status IN ('done', 'skipped')",
                (self.crawler,),
            )
            return {r[0] for r in cur.fetchall()}

    def add_pending(self, urls: list[str]) -> int:
        """Remember discovered-but-uncrawled URLs so a restart resumes the
        frontier instead of starting again from the seeds."""
        if not urls:
            return 0
        from psycopg2.extras import execute_values

        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO crawl_urls (url, crawler, status, crawled_at)
                VALUES %s
                ON CONFLICT (url) DO NOTHING
                """,
                [(u, self.crawler, "pending") for u in urls],
                template="(%s, %s, %s, NULL)",
                page_size=500,
            )
            return cur.rowcount

    def pending_urls(self, limit: int = 20000) -> list[str]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT url FROM crawl_urls WHERE crawler = %s AND status = 'pending' LIMIT %s",
                (self.crawler, limit),
            )
            return [r[0] for r in cur.fetchall()]

    def mark(self, url: str, status: str, chunks: int = 0, error: str | None = None) -> None:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crawl_urls (url, crawler, status, chunks_added, error, crawled_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (url) DO UPDATE SET
                    crawler = EXCLUDED.crawler, status = EXCLUDED.status,
                    chunks_added = EXCLUDED.chunks_added, error = EXCLUDED.error,
                    crawled_at = NOW()
                """,
                (url, self.crawler, status, chunks, error),
            )

    def counts(self) -> dict[str, Any]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*), COALESCE(SUM(chunks_added), 0)
                FROM crawl_urls WHERE crawler = %s GROUP BY status
                """,
                (self.crawler,),
            )
            rows = cur.fetchall()
        out: dict[str, Any] = {"by_status": {}, "chunks": 0}
        for status, n, chunks in rows:
            out["by_status"][status] = n
            out["chunks"] += int(chunks)
        return out
