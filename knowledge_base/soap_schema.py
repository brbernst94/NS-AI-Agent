"""Build the NetSuite record/field catalog from NetSuite's public SOAP schemas.

NetSuite publishes the complete SuiteTalk data model as WSDL + XSD at
webservices.netsuite.com, with no authentication. Every record type is an
`xsd:complexType` extending `platformCore:Record`, its fields are the
`xsd:element` children, and sublists are elements whose type is a `...List`
wrapper around a repeated item type.

This replaced the Records Browser crawler, which NetSuite took offline.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests

from knowledge_base.catalog import NetSuiteCatalog, SHARED

logger = logging.getLogger(__name__)

XSD_NS = "{http://www.w3.org/2001/XMLSchema}"
WSDL_BASE = "https://webservices.netsuite.com/wsdl/{version}/netsuite.wsdl"
DEFAULT_VERSION = os.getenv("NS_SOAP_VERSION", "v2024_1_0")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NS-AI-Agent/1.0; schema reader)"}
_TIMEOUT = 45

# Helper types that are machinery, not records.
_SKIP_SUFFIXES = (
    "SearchBasic", "SearchAdvanced", "SearchRow", "SearchRowBasic", "Search",
    "List", "Ref", "Response", "Request", "Result", "Filter", "Fields",
)
_SKIP_EXACT = {"Record", "RecordRef", "CustomRecordRef", "BaseRef", "CustomFieldRef"}

# XSD path fragment -> catalog category. Paths carry the version in the middle
# ("lists/v2024_1_0/relationships.xsd"), so match on the directory or the file
# name, never on a "dir/file" pair. Order matters: transactions/employees.xsd
# is a transaction, not an entity.
_CATEGORY_BY_PATH = [
    ("transactions/", "transaction"),
    ("relationships", "entity"),
    ("employees", "entity"),
    ("accounting", "list"),
    ("supplyChain", "list"),
    ("marketing", "list"),
    ("website", "list"),
    ("support", "support"),
    ("activities/", "activity"),
    ("communication", "activity"),
    ("documents/", "file"),
    ("customization", "custom"),
]

_TYPE_MAP = {
    "string": "text", "boolean": "checkbox", "double": "decimal", "decimal": "decimal",
    "float": "decimal", "int": "integer", "integer": "integer", "long": "integer",
    "dateTime": "datetime", "date": "date", "time": "time", "base64Binary": "file",
    "anyType": "text",
}


def _local(name: str | None) -> str:
    """'platformCore:RecordRef' -> 'RecordRef'; '{ns}element' -> 'element'."""
    if not name:
        return ""
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _humanize(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return s[:1].upper() + s[1:]


def _is_skippable(name: str) -> bool:
    return name in _SKIP_EXACT or name.endswith(_SKIP_SUFFIXES)


class SoapSchemaImporter:
    """Downloads the SuiteTalk schemas and populates the structured catalog."""

    def __init__(
        self,
        catalog: NetSuiteCatalog,
        version: str = DEFAULT_VERSION,
        tenant_id: str = SHARED,
    ) -> None:
        self.catalog = catalog
        self.version = version
        self.tenant_id = tenant_id
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        # name -> (element, source path)
        self.complex_types: dict[str, tuple[ET.Element, str]] = {}
        self.simple_types: dict[str, list[str]] = {}
        self.report: dict[str, Any] = {
            "version": version, "files": 0, "records": 0, "fields": 0,
            "sublists": 0, "sublist_fields": 0, "errors": [],
        }
        self.last_url: str | None = None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def schema_urls(self) -> list[str]:
        wsdl_url = WSDL_BASE.format(version=self.version)
        self.last_url = wsdl_url
        resp = self.session.get(wsdl_url, timeout=_TIMEOUT)
        resp.raise_for_status()
        locs = sorted(set(re.findall(r'schemaLocation="([^"]+)"', resp.text)))
        if not locs:
            raise RuntimeError(f"No schemaLocation entries found in {wsdl_url}")
        return [urljoin(wsdl_url, loc) for loc in locs]

    def load(self) -> None:
        """Fetch every XSD and index its complexTypes and enum simpleTypes."""
        for url in self.schema_urls():
            self.last_url = url
            try:
                resp = self.session.get(url, timeout=_TIMEOUT)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as exc:
                self.report["errors"].append(f"{url}: {exc}")
                logger.warning("Schema fetch/parse failed for %s: %s", url, exc)
                continue

            path = url.split("/xsd/", 1)[-1]
            for ct in root.iter(f"{XSD_NS}complexType"):
                name = ct.get("name")
                if name:
                    self.complex_types[name] = (ct, path)
            for st in root.iter(f"{XSD_NS}simpleType"):
                name = st.get("name")
                if not name:
                    continue
                values = [
                    e.get("value") for e in st.iter(f"{XSD_NS}enumeration") if e.get("value")
                ]
                if values:
                    self.simple_types[name] = values
            self.report["files"] += 1
        logger.info(
            "Loaded %d schema files: %d complexTypes, %d enums",
            self.report["files"], len(self.complex_types), len(self.simple_types),
        )

    # ------------------------------------------------------------------
    # Interpret
    # ------------------------------------------------------------------

    @staticmethod
    def _base_of(ct: ET.Element) -> str:
        ext = ct.find(f"{XSD_NS}complexContent/{XSD_NS}extension")
        return _local(ext.get("base")) if ext is not None else ""

    @staticmethod
    def _elements(ct: ET.Element) -> list[ET.Element]:
        return [e for e in ct.iter(f"{XSD_NS}element") if e.get("name")]

    def is_record(self, name: str, ct: ET.Element) -> bool:
        return self._base_of(ct) == "Record" and not _is_skippable(name)

    def _sublist_item_type(self, type_name: str) -> str | None:
        """If `type_name` is a `...List` wrapper, return the repeated item type."""
        entry = self.complex_types.get(type_name)
        if entry is None or not type_name.endswith("List"):
            return None
        for el in self._elements(entry[0]):
            if el.get("maxOccurs") in ("unbounded",) or el.get("maxOccurs", "1") not in ("0", "1"):
                item = _local(el.get("type"))
                if item in self.complex_types:
                    return item
        return None

    def _field(self, el: ET.Element) -> dict[str, Any]:
        name = el.get("name") or ""
        type_name = _local(el.get("type"))
        enum_values = self.simple_types.get(type_name)
        select_record = None

        if enum_values:
            ftype = "select"
        elif type_name in ("RecordRef", "CustomRecordRef", "BaseRef"):
            ftype = "select"
        elif type_name in _TYPE_MAP:
            ftype = _TYPE_MAP[type_name]
        elif type_name in self.complex_types:
            ftype = "subrecord"
            select_record = type_name.lower()
        else:
            ftype = "text"

        # RecordRef fields are usually named after what they point at.
        if ftype == "select" and select_record is None and not enum_values:
            guess = re.sub(r"(Ref|Id)$", "", name).lower()
            if guess in {k.lower() for k in self.complex_types}:
                select_record = guess

        return {
            "field_id": name,
            "label": _humanize(name),
            "type": ftype,
            # SuiteTalk marks nearly everything optional; treat an explicit
            # minOccurs of 1+ as the only reliable "required" signal.
            "required": el.get("minOccurs") not in (None, "0") or el.get("nillable") == "false",
            "select_record": select_record,
            "enum_values": enum_values,
        }

    def _category(self, path: str) -> str:
        for fragment, category in _CATEGORY_BY_PATH:
            if fragment in path:
                return category
        return "other"

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        if not self.complex_types:
            self.load()
        if not self.complex_types:
            raise RuntimeError(
                f"No schema types loaded from {WSDL_BASE.format(version=self.version)} — "
                "NetSuite's schemas may have moved."
            )

        # Refresh rather than accumulate: only once the schemas are safely in
        # hand, so a failed download never leaves the catalog empty.
        self.report["purged"] = self.catalog.purge_source("soap_schema", self.tenant_id)

        for name, (ct, path) in sorted(self.complex_types.items()):
            if not self.is_record(name, ct):
                continue
            try:
                self._import_record(name, ct, path)
            except Exception as exc:
                logger.exception("Failed importing %s", name)
                self.report["errors"].append(f"{name}: {exc}")

        logger.info(
            "SOAP schema import complete: %d records, %d fields, %d sublists",
            self.report["records"], self.report["fields"], self.report["sublists"],
        )
        return self.report

    def _import_record(self, name: str, ct: ET.Element, path: str) -> None:
        record_id = name.lower()
        seq = ct.find(f"{XSD_NS}complexContent/{XSD_NS}extension/{XSD_NS}sequence")
        elements = [e for e in (seq if seq is not None else ct) if _local(e.tag) == "element"]

        fields: list[dict[str, Any]] = []
        sublists: list[tuple[str, str, str]] = []  # (sublist_id, label, item_type)

        for el in elements:
            el_name = el.get("name")
            if not el_name:
                continue
            item_type = self._sublist_item_type(_local(el.get("type")))
            if item_type:
                # SOAP names the wrapper element "itemList"; SuiteScript and the
                # UI call that sublist "item". Store the SuiteScript-style id,
                # since that is what anyone writing a script actually needs, and
                # keep the SOAP name in the label for traceability.
                sublist_id = re.sub(r"List$", "", el_name) or el_name
                label = f"{_humanize(sublist_id)} (SOAP: {el_name})"
                sublists.append((sublist_id, label, item_type))
            else:
                fields.append(self._field(el))

        self.catalog.upsert_record_type(
            record_id,
            label=_humanize(name),
            category=self._category(path),
            source="soap_schema",
            version=self.version,
            tenant_id=self.tenant_id,
        )
        self.report["records"] += 1
        self.report["fields"] += self.catalog.upsert_fields(
            record_id, fields, source="soap_schema", tenant_id=self.tenant_id
        )

        for sublist_id, label, item_type in sublists:
            item_ct = self.complex_types.get(item_type)
            if item_ct is None:
                continue
            sub_fields = [self._field(e) for e in self._elements(item_ct[0])]
            n = self.catalog.upsert_sublist(
                record_id, sublist_id, label=label, fields=sub_fields,
                source="soap_schema", tenant_id=self.tenant_id,
            )
            self.report["sublists"] += 1
            self.report["sublist_fields"] += n

    # For CrawlManager progress reporting.
    def status(self) -> dict[str, Any]:
        return {
            "pages_crawled": self.report["files"],
            "records": self.report["records"],
            "queued": 0,
            "last_url": self.last_url,
            "version": self.version,
        }

    def crawl(self, max_records: int | None = None) -> int:
        """Same entry point name the crawl manager uses."""
        self.run()
        return self.report["records"]
