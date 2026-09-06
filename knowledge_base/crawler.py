"""NetSuite Help Center documentation crawler.

Discovers and ingests Oracle's public NetSuite documentation into the tagged
knowledge base. Crawl state lives in PostgreSQL so it resumes across deploys
and never re-ingests a page.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from knowledge_base.crawl_state import CrawlState

logger = logging.getLogger(__name__)

HELP_ROOT = "https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/"

SEED_URLS = [
    HELP_ROOT,
    HELP_ROOT + "index.html",
    HELP_ROOT + "netsuitecs_gs/NSCSGS.htm",
    HELP_ROOT + "netsuitecs_gs/NSSOI.htm",
    HELP_ROOT + "netsuitecs_gs/NSIMPT.htm",
    HELP_ROOT + "netsuitecs_gs/NSCOA.htm",
    HELP_ROOT + "netsuitecs_gs/NSOW.htm",
    HELP_ROOT + "netsuitecs_gs/NSCS.htm",
    HELP_ROOT + "netsuitecs_gs/NSITEM.htm",
    HELP_ROOT + "netsuitecs_gs/NSEMP.htm",
]

ALLOWED_DOMAINS = {"docs.oracle.com"}
ALLOWED_PATH_PREFIX = "/en/cloud/saas/netsuite/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NS-AI-Agent/1.0; knowledge crawler)",
    "Accept": "text/html,application/xhtml+xml",
}
_REQUEST_TIMEOUT = 20
_CRAWL_DELAY = float(os.getenv("CRAWL_DELAY_SECONDS", "1.0"))
_MIN_TEXT = 200

# Keyword -> module. Checked against title + URL, first match wins.
_MODULE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("suitescript", ("suitescript", "n/record", "n/search", "restlet", "map/reduce", "user event", "client script", "scheduled script")),
    ("suitetalk", ("suitetalk", "rest web services", "soap web services", "rest api", "web services")),
    ("suiteflow", ("suiteflow", "workflow")),
    ("suiteanalytics", ("suiteanalytics", "saved search", "suiteql", "workbook", "dataset", "analytics")),
    ("suitecloud", ("suitecloud", "sdf", "suitebundler", "bundle", "customization", "custom record", "custom field")),
    ("csv_import", ("csv import", "import assistant", "csv file", "importing")),
    ("accounting", ("accounting", "general ledger", "journal", "chart of accounts", "period", "close", "fixed asset", "amortization", "revenue recognition", "multi-book")),
    ("oneworld", ("oneworld", "subsidiar", "intercompany", "consolidat", "multi-currency", "currency")),
    ("order_management", ("sales order", "order management", "fulfillment", "invoice", "billing", "return authorization", "estimate", "quote")),
    ("inventory", ("inventory", "item", "assembly", "bin", "warehouse", "wms", "demand planning", "units of measure")),
    ("purchasing", ("purchase order", "vendor", "procurement", "receiving", "bill")),
    ("banking", ("bank", "payment", "deposit", "reconcil", "electronic payments")),
    ("tax", ("tax", "vat", "gst", "nexus", "1099")),
    ("crm", ("crm", "lead", "prospect", "opportunit", "campaign", "case", "support", "marketing")),
    ("projects", ("project", "job", "resource allocation", "time tracking", "timesheet")),
    ("payroll_hr", ("payroll", "employee", "hr", "human resources", "expense report", "paid time off")),
    ("ecommerce", ("suitecommerce", "web store", "webstore", "ecommerce", "site builder")),
    ("administration", ("setup", "administrator", "role", "permission", "users/roles", "company information", "enable features", "preferences", "security", "sso", "authentication")),
    ("reporting", ("report", "financial statement", "dashboard", "kpi")),
]


def _is_allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return (
            p.scheme in ("http", "https")
            and p.netloc in ALLOWED_DOMAINS
            and p.path.startswith(ALLOWED_PATH_PREFIX)
            and not p.path.lower().endswith((".pdf", ".zip", ".png", ".jpg", ".gif", ".css", ".js"))
        )
    except Exception:
        return False


def _extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for tag in soup.find_all("a", href=True):
        full = urljoin(base_url, tag["href"]).split("#")[0].split("?")[0]
        if _is_allowed_url(full):
            links.add(full)
    return sorted(links)


def _extract_page(html: str, url: str) -> tuple[str, str | None]:
    """Return (text, title) for a help page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["nav", "footer", "script", "style", "header", "aside", "noscript"]):
        tag.decompose()
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=lambda c: c and "content" in c.lower())
        or soup.body
    )
    if not main:
        return "", None
    title_tag = soup.find("title")
    title = _clean(title_tag.get_text()) if title_tag else None
    if title:
        title = re.sub(r"\s*[|\-–]\s*NetSuite.*$", "", title).strip() or title
    text = main.get_text(separator="\n", strip=True)
    return text, title


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def infer_module(title: str | None, url: str) -> str | None:
    hay = f"{title or ''} {url}".lower()
    for module, keys in _MODULE_RULES:
        if any(k in hay for k in keys):
            return module
    return None


class NetSuiteCrawler:
    """Crawls Oracle's public NetSuite Help Center into the knowledge base."""

    def __init__(
        self,
        knowledge_index: Any,
        db_path: str | None = None,  # legacy arg, ignored
        max_pages: int | None = None,
        seeds: list[str] | None = None,
        version: str | None = None,
    ) -> None:
        self.knowledge_index = knowledge_index
        self.state = CrawlState("docs")
        self.max_pages = max_pages or int(os.getenv("CRAWL_MAX_PAGES", "5000"))
        self.seeds = seeds or SEED_URLS
        self.version = version or os.getenv("NS_DOCS_VERSION")
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        # live progress
        self.pages_crawled = 0
        self.chunks_added = 0
        self.queued = 0
        self.last_url: str | None = None

    def crawl(self, max_pages: int | None = None, progress: Callable[[dict], None] | None = None) -> int:
        from knowledge_base.ingest import DocumentIngester

        ingester = DocumentIngester(self.knowledge_index)
        limit = max_pages or self.max_pages
        done = self.state.done_urls()
        queue: list[str] = [u for u in self.seeds if u not in done]
        seen: set[str] = set(queue) | done
        self.pages_crawled = 0
        self.chunks_added = 0

        logger.info("Docs crawl starting: %d seeds, %d already crawled, limit %d", len(queue), len(done), limit)

        while queue and self.pages_crawled < limit:
            url = queue.pop(0)
            self.last_url = url
            self.queued = len(queue)
            try:
                resp = self.session.get(url, timeout=_REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    self.state.mark(url, "failed", error=f"HTTP {resp.status_code}")
                    continue
                if "html" not in resp.headers.get("content-type", ""):
                    self.state.mark(url, "skipped", error="non-html")
                    continue

                html = resp.text
                text, title = _extract_page(html, url)

                # Discover links regardless of whether this page has body text
                for link in _extract_links(html, url):
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)

                if len(text) < _MIN_TEXT:
                    self.state.mark(url, "skipped", error="too short")
                    continue

                module = infer_module(title, url)
                chunks = ingester.ingest_text(
                    f"SOURCE: {title or url}\nURL: {url}\n\n{text}",
                    source_name=f"oracle_docs:{url}",
                    tags={
                        "system": "netsuite", "doc_type": "help", "module": module,
                        "title": title, "url": url, "version": self.version,
                    },
                )
                self.state.mark(url, "done", chunks=chunks)
                self.pages_crawled += 1
                self.chunks_added += chunks
                logger.info("Crawled %d/%d [%s]: %s (%d chunks)", self.pages_crawled, limit, module or "-", url, chunks)
                if progress:
                    progress(self.status())
            except Exception as exc:
                logger.warning("Failed to crawl %s: %s", url, exc)
                self.state.mark(url, "failed", error=str(exc)[:500])
            time.sleep(_CRAWL_DELAY)

        logger.info("Docs crawl finished: %d pages, %d chunks, %d still queued", self.pages_crawled, self.chunks_added, len(queue))
        return self.chunks_added

    def status(self) -> dict[str, Any]:
        return {
            "pages_crawled": self.pages_crawled,
            "chunks_added": self.chunks_added,
            "queued": self.queued,
            "last_url": self.last_url,
            "limit": self.max_pages,
        }
