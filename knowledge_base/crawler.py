"""
NetSuite documentation crawler.

Automatically discovers and ingests Oracle/NetSuite public documentation
into the knowledge base. Tracks crawled URLs to avoid duplication.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Public NetSuite/Oracle documentation entry points
SEED_URLS = [
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSCSGS.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSSOI.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSIMPT.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSCOA.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSOW.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSCS.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSITEM.htm",
    "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/netsuitecs_gs/NSEMP.htm",
]

ALLOWED_DOMAINS = {"docs.oracle.com"}
ALLOWED_PATH_PREFIX = "/en/cloud/saas/netsuite/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NS-AI-Agent/1.0; educational crawler)",
    "Accept": "text/html,application/xhtml+xml",
}

_REQUEST_TIMEOUT = 15
_CRAWL_DELAY = 1.5  # seconds between requests


def _is_allowed_url(url: str) -> bool:
    """Check if URL is within Oracle NetSuite docs."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in ("http", "https")
            and parsed.netloc in ALLOWED_DOMAINS
            and parsed.path.startswith(ALLOWED_PATH_PREFIX)
        )
    except Exception:
        return False


def _extract_links(html: str, base_url: str) -> list[str]:
    """Extract internal links from a page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        full_url = urljoin(base_url, href).split("#")[0]  # Remove fragments
        if _is_allowed_url(full_url):
            links.append(full_url)
    return list(set(links))


def _extract_text(html: str, url: str) -> str:
    """Extract meaningful text from a NetSuite docs page."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove nav, footer, scripts, styles
    for tag in soup(["nav", "footer", "script", "style", "header", "aside"]):
        tag.decompose()

    # Try to get main content area
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=lambda c: c and "content" in c.lower())
        or soup.body
    )

    if not main:
        return ""

    text = main.get_text(separator="\n", strip=True)

    # Add source context
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url

    return f"SOURCE: {title}\nURL: {url}\n\n{text}"


class NetSuiteCrawler:
    """
    Crawls Oracle/NetSuite public documentation and ingests into knowledge base.

    Tracks visited URLs in SQLite to avoid re-crawling on subsequent startups.
    """

    def __init__(self, knowledge_index: object, db_path: str = "./data/crawler.db") -> None:
        self.knowledge_index = knowledge_index
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crawled_urls (
                    url TEXT PRIMARY KEY,
                    crawled_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    chunks_added INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    def _is_crawled(self, url: str) -> bool:
        # Check the knowledge index for an existing entry from this URL so
        # deduplication survives across restarts (crawler.db is ephemeral /tmp).
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM crawled_urls WHERE url = ?", (url,)).fetchone()
            if row:
                return True
        source_name = f"oracle_docs:{url}"
        return source_name in self.knowledge_index.list_sources()

    def _mark_crawled(self, url: str, chunks: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO crawled_urls (url, chunks_added) VALUES (?, ?)",
                (url, chunks),
            )
            conn.commit()

    def crawl(self, max_pages: int = 200) -> int:
        """
        Crawl NetSuite docs starting from seed URLs.

        Returns total number of chunks added.
        """
        from knowledge_base.ingest import DocumentIngester

        ingester = DocumentIngester(self.knowledge_index)
        queue = list(SEED_URLS)
        visited: set[str] = set()
        total_chunks = 0
        pages_crawled = 0

        logger.info("Starting NetSuite documentation crawl. Max pages: %d", max_pages)

        while queue and pages_crawled < max_pages:
            url = queue.pop(0)

            if url in visited or self._is_crawled(url):
                continue

            visited.add(url)

            try:
                resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    logger.debug("Skipping %s (status %d)", url, resp.status_code)
                    continue

                content_type = resp.headers.get("content-type", "")
                if "html" not in content_type:
                    continue

                html = resp.text
                text = _extract_text(html, url)

                if len(text) > 200:
                    chunks = ingester.ingest_text(text, source_name=f"oracle_docs:{url}")
                    total_chunks += chunks
                    pages_crawled += 1
                    logger.info("Crawled page %d/%d: %s (%d chunks)", pages_crawled, max_pages, url, chunks)
                    self._mark_crawled(url, chunks)

                    # Discover more links
                    new_links = _extract_links(html, url)
                    for link in new_links:
                        if link not in visited and not self._is_crawled(link):
                            queue.append(link)

                time.sleep(_CRAWL_DELAY)

            except Exception as exc:
                logger.warning("Failed to crawl %s: %s", url, exc)
                continue

        logger.info("Crawl complete. Pages: %d, Chunks added: %d", pages_crawled, total_chunks)
        return total_chunks
