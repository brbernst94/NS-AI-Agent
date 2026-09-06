"""Crawler progress tracking and stall diagnosis.

Answers one question without a human reading logs: is the knowledge base still
growing, and if not, why not. `diagnose()` compares current counts against a
snapshot from N hours ago, folds in per-crawler run history and the actual
error strings, and returns a verdict plus the evidence behind it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from knowledge_base import db

logger = logging.getLogger(__name__)

CRAWLERS = ("docs", "catalog")
_MIN_SNAPSHOT_GAP = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Counts and snapshots
# ---------------------------------------------------------------------------

def current_counts() -> dict[str, int]:
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM knowledge_documents")
        kb = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ns_record_types")
        records = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ns_fields")
        fields = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(status, 'unknown'), COUNT(*) FROM crawl_urls GROUP BY 1")
        by_status = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "kb_chunks": kb,
        "catalog_records": records,
        "catalog_fields": fields,
        "crawl_done": by_status.get("done", 0),
        "crawl_failed": by_status.get("failed", 0),
        "crawl_skipped": by_status.get("skipped", 0),
    }


def record_snapshot(reason: str = "periodic", force: bool = False) -> dict[str, Any] | None:
    """Store current counts. Skipped if a snapshot was taken very recently."""
    counts = current_counts()
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        if not force:
            cur.execute("SELECT taken_at FROM crawl_snapshots ORDER BY taken_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0] and (_now() - row[0]) < _MIN_SNAPSHOT_GAP:
                return None
        cur.execute(
            """
            INSERT INTO crawl_snapshots
                (reason, kb_chunks, catalog_records, catalog_fields,
                 crawl_done, crawl_failed, crawl_skipped)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING taken_at
            """,
            (
                reason, counts["kb_chunks"], counts["catalog_records"], counts["catalog_fields"],
                counts["crawl_done"], counts["crawl_failed"], counts["crawl_skipped"],
            ),
        )
        taken_at = cur.fetchone()[0]
    return {"taken_at": taken_at.isoformat(), "reason": reason, **counts}


def _baseline(window_hours: float) -> tuple[dict[str, Any] | None, float | None]:
    """Most recent snapshot at least `window_hours` old, else the oldest we have."""
    cutoff = _now() - timedelta(hours=window_hours)
    cols = "taken_at, kb_chunks, catalog_records, catalog_fields, crawl_done, crawl_failed, crawl_skipped"
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM crawl_snapshots WHERE taken_at <= %s ORDER BY taken_at DESC LIMIT 1",
            (cutoff,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(f"SELECT {cols} FROM crawl_snapshots ORDER BY taken_at ASC LIMIT 1")
            row = cur.fetchone()
    if row is None:
        return None, None
    snap = {
        "taken_at": row[0].isoformat(), "kb_chunks": row[1], "catalog_records": row[2],
        "catalog_fields": row[3], "crawl_done": row[4], "crawl_failed": row[5],
        "crawl_skipped": row[6],
    }
    age_hours = round((_now() - row[0]).total_seconds() / 3600, 2)
    return snap, age_hours


# ---------------------------------------------------------------------------
# Run bookkeeping (written by CrawlManager)
# ---------------------------------------------------------------------------

def start_run(crawler: str) -> int:
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crawl_runs (crawler, state) VALUES (%s, 'running') RETURNING id",
            (crawler,),
        )
        return cur.fetchone()[0]


def finish_run(
    run_id: int | None,
    state: str,
    pages: int = 0,
    chunks: int = 0,
    queue_remaining: int | None = None,
    error: str | None = None,
) -> None:
    if run_id is None:
        return
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET state = %s, finished_at = NOW(), pages = %s, chunks = %s,
                queue_remaining = %s, error = %s
            WHERE id = %s
            """,
            (state, pages, chunks, queue_remaining, (error or None), run_id),
        )


def last_runs() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        for crawler in CRAWLERS:
            cur.execute(
                """
                SELECT id, state, started_at, finished_at, pages, chunks, queue_remaining, error
                FROM crawl_runs WHERE crawler = %s ORDER BY started_at DESC LIMIT 1
                """,
                (crawler,),
            )
            row = cur.fetchone()
            if row:
                out[crawler] = {
                    "id": row[0], "state": row[1],
                    "started_at": row[2].isoformat() if row[2] else None,
                    "finished_at": row[3].isoformat() if row[3] else None,
                    "pages": row[4], "chunks": row[5],
                    "queue_remaining": row[6], "error": row[7],
                }
    return out


# ---------------------------------------------------------------------------
# Per-crawler URL detail
# ---------------------------------------------------------------------------

def crawler_detail() -> dict[str, dict[str, Any]]:
    detail: dict[str, dict[str, Any]] = {
        c: {"by_status": {}, "last_success": None, "top_errors": []} for c in CRAWLERS
    }
    with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(crawler,'unknown'), COALESCE(status,'unknown'), COUNT(*) "
            "FROM crawl_urls GROUP BY 1, 2"
        )
        for crawler, status, n in cur.fetchall():
            detail.setdefault(crawler, {"by_status": {}, "last_success": None, "top_errors": []})
            detail[crawler]["by_status"][status] = n

        cur.execute(
            "SELECT COALESCE(crawler,'unknown'), MAX(crawled_at) FROM crawl_urls "
            "WHERE status = 'done' GROUP BY 1"
        )
        for crawler, ts in cur.fetchall():
            if crawler in detail and ts:
                detail[crawler]["last_success"] = ts.isoformat()

        cur.execute(
            """
            SELECT COALESCE(crawler,'unknown'), LEFT(COALESCE(error, '(none)'), 160), COUNT(*)
            FROM crawl_urls WHERE status IN ('failed', 'skipped')
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20
            """
        )
        for crawler, err, n in cur.fetchall():
            if crawler in detail and len(detail[crawler]["top_errors"]) < 6:
                detail[crawler]["top_errors"].append({"error": err, "count": n})
    return detail


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def _verdict_for(
    crawler: str,
    detail: dict[str, Any],
    run: dict[str, Any] | None,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    # The catalog importer reads NetSuite's SOAP schemas rather than crawling
    # URLs, so it is judged on records imported, not on crawl_urls rows.
    if crawler == "catalog":
        return _catalog_verdict(run, counts or {})

    by_status = detail.get("by_status", {})
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    skipped = by_status.get("skipped", 0)
    attempted = done + failed + skipped

    if run and run.get("state") == "running":
        return {"verdict": "running", "reason": "A run is in progress."}

    if attempted == 0:
        if run is None:
            return {
                "verdict": "never_ran",
                "reason": "No run has ever been recorded for this crawler.",
                "action": "Check that startup completed and CRAWL_ON_STARTUP is not 0, then POST /knowledge/crawl/start.",
            }
        return {
            "verdict": "no_urls_attempted",
            "reason": "A run happened but no URLs were attempted — discovery produced nothing.",
            "action": "The entry-point URL is likely wrong or unreachable. Needs a code fix.",
        }

    if done == 0:
        return {
            "verdict": "failing",
            "reason": f"{attempted} URLs attempted, none succeeded ({failed} failed, {skipped} skipped).",
            "action": "Every fetch or parse is failing. Read top_errors and fix the crawler.",
        }

    if run and run.get("state") == "failed":
        return {
            "verdict": "run_error",
            "reason": f"Last run raised: {str(run.get('error'))[:200]}",
            "action": "Fix the exception, then restart the crawl.",
        }

    queue_remaining = (run or {}).get("queue_remaining")
    if run and run.get("state") == "done" and queue_remaining == 0:
        return {
            "verdict": "complete",
            "reason": f"Last run finished with an empty queue after {done} successful URLs.",
        }

    if run and run.get("state") == "done" and (run.get("pages") or 0) == 0 and queue_remaining:
        return {
            "verdict": "stalled",
            "reason": f"Last run added 0 new pages but {queue_remaining} URLs remain queued.",
            "action": "Pages are being fetched but rejected. Read top_errors.",
        }

    return {"verdict": "progressing", "reason": f"{done} URLs succeeded, {failed} failed, {skipped} skipped."}


def _catalog_verdict(run: dict[str, Any] | None, counts: dict[str, int]) -> dict[str, Any]:
    records = counts.get("catalog_records", 0)
    fields = counts.get("catalog_fields", 0)
    if run and run.get("state") == "running":
        return {"verdict": "running", "reason": "Schema import in progress."}
    if run is None:
        return {
            "verdict": "never_ran",
            "reason": "The catalog importer has never run.",
            "action": "POST /knowledge/crawl/start with target 'catalog'.",
        }
    if run.get("state") == "failed":
        return {
            "verdict": "run_error",
            "reason": f"Last import raised: {str(run.get('error'))[:250]}",
            "action": "NetSuite's SOAP schemas may have moved; check knowledge_base/soap_schema.py.",
        }
    if records == 0:
        return {
            "verdict": "failing",
            "reason": "The import finished but produced no record types.",
            "action": "The schema layout has probably changed. Re-check the parser against a live XSD.",
        }
    return {
        "verdict": "complete",
        "reason": f"{records} record types and {fields} fields imported.",
    }


def diagnose(window_hours: float = 6.0) -> dict[str, Any]:
    """Whole-system verdict on whether the knowledge base is still growing."""
    counts = current_counts()
    baseline, baseline_age = _baseline(window_hours)
    detail = crawler_detail()
    runs = last_runs()

    growth: dict[str, int] | None = None
    total_growth = 0
    if baseline:
        growth = {k: counts[k] - baseline.get(k, 0) for k in counts}
        total_growth = growth["kb_chunks"] + growth["catalog_records"] + growth["catalog_fields"]

    per_crawler = {c: _verdict_for(c, detail.get(c, {}), runs.get(c), counts) for c in CRAWLERS}

    broken = [c for c, v in per_crawler.items()
              if v["verdict"] in ("failing", "never_ran", "no_urls_attempted", "run_error", "stalled")]
    running = [c for c, v in per_crawler.items() if v["verdict"] == "running"]

    attempts = counts["crawl_done"] + counts["crawl_failed"] + counts["crawl_skipped"]
    nothing_ever = (
        counts["kb_chunks"] == 0 and counts["catalog_records"] == 0 and attempts == 0 and not runs
    )

    # Order matters: a broken crawler is the most actionable finding, so it
    # outranks growth elsewhere. "empty" is only for a truly untouched system.
    if nothing_ever:
        verdict = "empty"
        headline = "Nothing has been crawled or ingested yet — no run has ever started."
    elif broken:
        verdict = "needs_attention"
        headline = "Broken: " + "; ".join(f"{c} ({per_crawler[c]['verdict']})" for c in broken)
    elif running:
        verdict = "running"
        headline = f"{', '.join(running)} running now."
    elif baseline is None:
        verdict = "no_history"
        headline = "No earlier snapshot to compare against; this call recorded the first one."
    elif total_growth > 0:
        verdict = "healthy"
        headline = (
            f"Grew by {growth['kb_chunks']} chunks, {growth['catalog_records']} records, "
            f"{growth['catalog_fields']} fields in the last {baseline_age}h."
        )
    elif all(per_crawler[c]["verdict"] == "complete" for c in CRAWLERS):
        verdict = "complete"
        headline = "Both crawlers finished their queues; no growth expected without new sources."
    else:
        verdict = "needs_attention"
        headline = f"No growth in the last {baseline_age}h and nothing is running."

    lines = [f"VERDICT: {verdict} — {headline}",
             f"totals: {counts['kb_chunks']} chunks, {counts['catalog_records']} records, "
             f"{counts['catalog_fields']} fields"]
    for c in CRAWLERS:
        v = per_crawler[c]
        lines.append(f"{c}: {v['verdict']} — {v['reason']}")
        for e in detail.get(c, {}).get("top_errors", [])[:3]:
            lines.append(f"    {e['count']}x {e['error']}")

    # Keep the trend series alive for the next call.
    try:
        record_snapshot("diagnose")
    except Exception as exc:  # never let bookkeeping break the report
        logger.warning("Snapshot write failed: %s", exc)

    return {
        "verdict": verdict,
        "headline": headline,
        "summary": "\n".join(lines),
        "window_hours": window_hours,
        "checked_at": _now().isoformat(),
        "counts": counts,
        "baseline": baseline,
        "baseline_age_hours": baseline_age,
        "growth": growth,
        "per_crawler": per_crawler,
        "detail": detail,
        "last_runs": runs,
    }
