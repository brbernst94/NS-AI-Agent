"""Runs knowledge crawlers in a background thread and reports progress."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TARGETS = ("docs", "catalog", "all")


class CrawlManager:
    def __init__(self, knowledge_index: Any, catalog: Any) -> None:
        self.knowledge_index = knowledge_index
        self.catalog = catalog
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: Any = None
        self.jobs: dict[str, dict[str, Any]] = {
            "docs": {"state": "idle"},
            "catalog": {"state": "idle"},
        }

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, target: str = "all", max_pages: int | None = None) -> dict[str, Any]:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        with self._lock:
            if self.running:
                return {"started": False, "reason": "a crawl is already running", **self.status()}
            targets = ["catalog", "docs"] if target == "all" else [target]
            self._thread = threading.Thread(
                target=self._run, args=(targets, max_pages), daemon=True, name="knowledge-crawler"
            )
            self._thread.start()
        return {"started": True, "targets": targets, **self.status()}

    def _run(self, targets: list[str], max_pages: int | None) -> None:
        from knowledge_base import crawl_health

        for t in targets:
            job = self.jobs[t]
            job.update({"state": "running", "started_at": _now(), "finished_at": None, "error": None})
            run_id = None
            crawler = None
            try:
                run_id = crawl_health.start_run(t)
            except Exception as exc:
                logger.warning("Could not record run start for %s: %s", t, exc)
            try:
                if t == "catalog":
                    from knowledge_base.soap_schema import SoapSchemaImporter

                    crawler = SoapSchemaImporter(self.catalog)
                    self._current = crawler
                    n = crawler.crawl()
                    job["result"] = {"records": n, **crawler.report}
                else:
                    from knowledge_base.crawler import NetSuiteCrawler

                    crawler = NetSuiteCrawler(self.knowledge_index, max_pages=max_pages)
                    self._current = crawler
                    n = crawler.crawl()
                    job["result"] = {"chunks_added": n, **crawler.status()}
                job["state"] = "done"
                self._finish_run(crawl_health, run_id, "done", crawler)
            except Exception as exc:
                logger.exception("Crawler %s failed", t)
                job.update({"state": "failed", "error": str(exc)[:500]})
                self._finish_run(crawl_health, run_id, "failed", crawler, error=str(exc)[:500])
            finally:
                job["finished_at"] = _now()
                self._current = None
                try:
                    crawl_health.record_snapshot(f"after_{t}", force=True)
                except Exception as exc:
                    logger.warning("Snapshot after %s failed: %s", t, exc)

    @staticmethod
    def _finish_run(crawl_health: Any, run_id: int | None, state: str, crawler: Any, error: str | None = None) -> None:
        status = {}
        if crawler is not None:
            live = getattr(crawler, "status", None)
            if callable(live):
                try:
                    status = live() or {}
                except Exception:
                    status = {}
        try:
            crawl_health.finish_run(
                run_id, state,
                pages=int(status.get("pages_crawled") or status.get("pages") or 0),
                chunks=int(status.get("chunks_added") or 0),
                queue_remaining=status.get("queued"),
                error=error,
            )
        except Exception as exc:
            logger.warning("Could not record run finish: %s", exc)

    def status(self) -> dict[str, Any]:
        from knowledge_base.crawl_state import CrawlState

        out: dict[str, Any] = {"running": self.running, "jobs": {}}
        for name, job in self.jobs.items():
            entry = dict(job)
            entry["totals"] = CrawlState(name).counts()
            if job.get("state") == "running" and self._current is not None:
                live = getattr(self._current, "status", None)
                if callable(live):
                    entry["progress"] = live()
                else:
                    entry["progress"] = {
                        "pages": getattr(self._current, "pages", None),
                        "last_url": getattr(self._current, "last_url", None),
                    }
            out["jobs"][name] = entry
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
