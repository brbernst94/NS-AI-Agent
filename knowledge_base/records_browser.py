"""Crawl the public NetSuite Records Browser into the structured catalog.

The Records Browser (system.netsuite.com/help/helpcenter/en_US/srbrowser/...)
lists every SuiteScript-accessible record with its fields, sublists and search
metadata. It requires no login, so this runs unattended on the server.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from knowledge_base.catalog import NetSuiteCatalog, SHARED
from knowledge_base.crawl_state import CrawlState

logger = logging.getLogger(__name__)

_BASE = "https://system.netsuite.com/help/helpcenter/en_US/srbrowser/"
# Newest first; the first index that responds 200 is used.
_VERSION_CANDIDATES = ["2026_2", "2026_1", "2025_2", "2025_1", "2024_2", "2024_1"]
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NS-AI-Agent/1.0; knowledge crawler)",
    "Accept": "text/html,application/xhtml+xml",
}
_DELAY = float(os.getenv("CRAWL_DELAY_SECONDS", "1.0"))
_TIMEOUT = 30


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _truthy(s: str) -> bool:
    return _clean(s).lower() in ("true", "yes", "y", "t", "required", "✓", "x")


class RecordsBrowserCrawler:
    def __init__(self, catalog: NetSuiteCatalog, version: str | None = None) -> None:
        self.catalog = catalog
        self.state = CrawlState("records_browser")
        self.version = version or os.getenv("NS_RECORDS_BROWSER_VERSION")
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self.pages = 0
        self.records = 0
        self.queued = 0
        self.last_url: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "pages_crawled": self.pages,
            "records": self.records,
            "queued": self.queued,
            "last_url": self.last_url,
            "version": self.version,
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _resolve_index(self) -> str | None:
        candidates = [self.version] if self.version else _VERSION_CANDIDATES
        for v in candidates:
            url = f"{_BASE}Browser{v}/script/record/"
            try:
                r = self.session.get(url, timeout=_TIMEOUT)
            except Exception as exc:
                logger.debug("Records Browser %s unreachable: %s", v, exc)
                continue
            if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                self.version = v
                logger.info("Using Records Browser version %s", v)
                return url
        logger.warning("No Records Browser index reachable (tried %s)", candidates)
        return None

    def _record_links(self, index_url: str, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0]
            if not href.endswith(".html"):
                continue
            full = urljoin(index_url, href)
            if "/script/record/" in full and not full.endswith("/index.html"):
                links.add(full)
        return sorted(links)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _table_rows(table: Tag) -> tuple[list[str], list[list[str]]]:
        headers: list[str] = []
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            ths = tr.find_all("th")
            if ths and not headers:
                headers = [_clean(th.get_text(" ")).lower() for th in ths]
                continue
            tds = tr.find_all("td")
            if tds:
                rows.append([_clean(td.get_text(" ")) for td in tds])
        if not headers and rows:
            # Some tables use a bold first row instead of <th>
            headers = [h.lower() for h in rows.pop(0)]
        return headers, rows

    @staticmethod
    def _col(headers: list[str], *names: str) -> int | None:
        for i, h in enumerate(headers):
            for n in names:
                if n in h:
                    return i
        return None

    def _fields_from_table(self, table: Tag) -> list[dict[str, Any]] | None:
        headers, rows = self._table_rows(table)
        if not headers:
            return None
        c_id = self._col(headers, "internal id", "internalid", "id")
        c_type = self._col(headers, "type")
        c_label = self._col(headers, "label", "name")
        if c_id is None or c_type is None:
            return None
        c_req = self._col(headers, "required")
        c_help = self._col(headers, "help", "description", "notes")
        c_ref = self._col(headers, "record", "reference", "select")
        fields = []
        for r in rows:
            if c_id >= len(r) or not r[c_id]:
                continue
            ftype = r[c_type] if c_type < len(r) else None
            select_record = None
            if c_ref is not None and c_ref < len(r) and r[c_ref]:
                select_record = r[c_ref].lower()
            elif ftype and "(" in ftype:
                # e.g. "select (customer)"
                m = re.search(r"\(([a-z0-9_]+)\)", ftype.lower())
                if m:
                    select_record = m.group(1)
            fields.append({
                "field_id": r[c_id].lower(),
                "label": r[c_label] if c_label is not None and c_label < len(r) else None,
                "type": (ftype or "").split("(")[0].strip().lower() or None,
                "required": _truthy(r[c_req]) if c_req is not None and c_req < len(r) else False,
                "select_record": select_record,
                "help": r[c_help] if c_help is not None and c_help < len(r) and r[c_help] else None,
            })
        return fields

    def _parse_record_page(self, url: str, html: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        label = _clean(h1.get_text(" ")) if h1 else None

        text = soup.get_text("\n")
        m = re.search(r"Internal ID\s*[:\-]?\s*([a-zA-Z0-9_]+)", text)
        record_id = m.group(1).lower() if m else url.rsplit("/", 1)[-1].replace(".html", "").lower()
        if not record_id:
            return None

        main_fields: list[dict[str, Any]] = []
        sublists: dict[str, dict[str, Any]] = {}
        section = ""
        sub = ""
        for el in soup.find_all(["h1", "h2", "h3", "h4", "table"]):
            if el.name in ("h1", "h2"):
                section = _clean(el.get_text(" ")).lower()
                sub = ""
            elif el.name in ("h3", "h4"):
                sub = _clean(el.get_text(" "))
                if not section:
                    section = sub.lower()
            elif el.name == "table":
                fields = self._fields_from_table(el)
                if not fields:
                    continue
                if "sublist" in section:
                    sid = re.sub(r"[^a-z0-9_]", "", sub.lower()) or f"sublist{len(sublists) + 1}"
                    entry = sublists.setdefault(sid, {"label": sub or sid, "fields": []})
                    entry["fields"].extend(fields)
                elif section.startswith("field") or section == "" or "field" in section:
                    if "search" in section or "filter" in section or "column" in section or "join" in section:
                        continue
                    main_fields.extend(fields)
                # Search joins/filters/columns, transform types, preferences: skipped for now.

        return {
            "id": record_id, "label": label, "url": url,
            "fields": main_fields, "sublists": sublists,
        }

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def crawl(self, max_records: int | None = None) -> int:
        index_url = self._resolve_index()
        if not index_url:
            return 0
        try:
            r = self.session.get(index_url, timeout=_TIMEOUT)
            links = self._record_links(index_url, r.text)
        except Exception as exc:
            logger.warning("Records Browser index fetch failed: %s", exc)
            return 0
        logger.info("Records Browser: %d record pages discovered", len(links))
        if max_records:
            links = links[:max_records]

        pending = [u for u in links if not self.state.is_done(u)]
        self.queued = len(pending)
        for url in pending:
            self.queued -= 1
            self.last_url = url
            try:
                resp = self.session.get(url, timeout=_TIMEOUT)
                if resp.status_code != 200:
                    self.state.mark(url, "failed", error=f"HTTP {resp.status_code}")
                    continue
                parsed = self._parse_record_page(url, resp.text)
                if not parsed or (not parsed["fields"] and not parsed["sublists"]):
                    self.state.mark(url, "skipped", error="no field tables found")
                    logger.info("Records Browser: no tables parsed on %s", url)
                    continue
                self._store(parsed)
                self.state.mark(url, "done", chunks=len(parsed["fields"]))
                self.pages += 1
                self.records += 1
                logger.info(
                    "Records Browser: %s — %d fields, %d sublists",
                    parsed["id"], len(parsed["fields"]), len(parsed["sublists"]),
                )
            except Exception as exc:
                logger.warning("Records Browser: failed %s: %s", url, exc)
                self.state.mark(url, "failed", error=str(exc)[:500])
            time.sleep(_DELAY)

        logger.info("Records Browser crawl complete: %d records", self.records)
        return self.records

    def _store(self, rec: dict[str, Any]) -> None:
        self.catalog.upsert_record_type(
            rec["id"], label=rec["label"], url=rec["url"],
            source="records_browser", version=self.version, tenant_id=SHARED,
        )
        self.catalog.upsert_fields(rec["id"], rec["fields"], source="records_browser")
        for sid, sl in rec["sublists"].items():
            self.catalog.upsert_sublist(
                rec["id"], sid, label=sl["label"], fields=sl["fields"], source="records_browser"
            )
