"""Import NetSuite REST metadata-catalog JSON Schemas into the structured catalog.

Input is the output of tools/netsuite_extract.py: a zip (or directory) of
`<schemaName>.json` files, one per record type plus the sublist collection /
element schemas they reference. The REST catalog describes every field NetSuite
exposes on a record, including required flags, enums, formats, and custom fields.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from knowledge_base.catalog import NetSuiteCatalog, SHARED, is_custom_field

logger = logging.getLogger(__name__)

_SKIP_SCHEMAS = {"nsresource", "nslink", "nserror", "nsresourcecollection"}


def _ref_name(ref: str | None) -> str | None:
    """'/services/rest/record/v1/metadata-catalog/salesOrder-itemCollection' -> 'salesOrder-itemCollection'."""
    if not ref:
        return None
    return ref.rstrip("/").split("/")[-1].split("#")[0] or None


def _map_type(prop: dict[str, Any]) -> str:
    t = prop.get("type")
    fmt = prop.get("format")
    if "enum" in prop:
        return "select"
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0] if t else None)
    if t == "string":
        if fmt == "date":
            return "date"
        if fmt == "date-time":
            return "datetime"
        return "text"
    if t == "integer":
        return "integer"
    if t == "number":
        return "decimal"
    if t == "boolean":
        return "checkbox"
    if t == "object" or "$ref" in prop:
        return "select"
    if t == "array":
        return "multiselect"
    return t or "unknown"


class MetadataImporter:
    def __init__(self, catalog: NetSuiteCatalog, tenant_id: str = SHARED, version: str | None = None):
        self.catalog = catalog
        self.tenant_id = tenant_id
        self.version = version
        self.schemas: dict[str, dict[str, Any]] = {}
        self.report: dict[str, Any] = {"records": 0, "fields": 0, "sublists": 0,
                                       "sublist_fields": 0, "skipped": [], "errors": []}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_zip(self, data: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".json"):
                    continue
                try:
                    self._add_schema(Path(name).stem, json.loads(zf.read(name)))
                except Exception as exc:
                    self.report["errors"].append(f"{name}: {exc}")

    def load_dir(self, path: str | Path) -> None:
        for p in Path(path).rglob("*.json"):
            try:
                self._add_schema(p.stem, json.loads(p.read_text(encoding="utf-8")))
            except Exception as exc:
                self.report["errors"].append(f"{p.name}: {exc}")

    def _add_schema(self, name: str, schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            return
        # The extractor may wrap as {"name":..., "schema":...}
        if "schema" in schema and isinstance(schema["schema"], dict) and "properties" not in schema:
            name = schema.get("name", name)
            schema = schema["schema"]
        self.schemas[name] = schema

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        record_names = [n for n in self.schemas if "-" not in n and n.lower() not in _SKIP_SCHEMAS]
        for name in sorted(record_names):
            try:
                self._import_record(name, self.schemas[name])
            except Exception as exc:
                logger.exception("Failed importing %s", name)
                self.report["errors"].append(f"{name}: {exc}")
        logger.info("Metadata import complete: %s", {k: v for k, v in self.report.items() if k not in ("skipped", "errors")})
        return self.report

    def _import_record(self, name: str, schema: dict[str, Any]) -> None:
        props = schema.get("properties") or {}
        if not props:
            self.report["skipped"].append(name)
            return
        record_id = name.lower()
        required = {r.lower() for r in (schema.get("required") or [])}

        self.catalog.upsert_record_type(
            record_id,
            label=schema.get("title") or _humanize(name),
            description=schema.get("description"),
            source="rest_metadata",
            version=self.version,
            tenant_id=self.tenant_id,
        )
        self.report["records"] += 1

        fields: list[dict[str, Any]] = []
        for prop_name, prop in props.items():
            if not isinstance(prop, dict):
                continue
            if prop_name in ("links", "refName"):
                continue
            sublist = self._as_sublist(prop)
            if sublist is not None:
                self._import_sublist(record_id, prop_name, prop, sublist)
                continue
            fields.append(self._field_from_prop(prop_name, prop, required))

        self.report["fields"] += self.catalog.upsert_fields(
            record_id, fields, source="rest_metadata", tenant_id=self.tenant_id
        )

    def _as_sublist(self, prop: dict[str, Any]) -> dict[str, Any] | None:
        """Return the element schema if this property is a sublist collection."""
        ref = _ref_name(prop.get("$ref"))
        target = self.schemas.get(ref) if ref else None
        candidate = target or prop
        items_prop = (candidate.get("properties") or {}).get("items")
        if not isinstance(items_prop, dict) or items_prop.get("type") != "array":
            return None
        element = items_prop.get("items") or {}
        elem_ref = _ref_name(element.get("$ref"))
        if elem_ref and elem_ref in self.schemas:
            return self.schemas[elem_ref]
        if element.get("properties"):
            return element
        return None

    def _import_sublist(self, record_id: str, sublist_id: str, prop: dict[str, Any], element: dict[str, Any]) -> None:
        required = {r.lower() for r in (element.get("required") or [])}
        fields = [
            self._field_from_prop(pn, pp, required)
            for pn, pp in (element.get("properties") or {}).items()
            if isinstance(pp, dict) and pn not in ("links", "refName")
        ]
        n = self.catalog.upsert_sublist(
            record_id, sublist_id,
            label=prop.get("title") or element.get("title") or _humanize(sublist_id),
            fields=fields, source="rest_metadata", tenant_id=self.tenant_id,
        )
        self.report["sublists"] += 1
        self.report["sublist_fields"] += n

    @staticmethod
    def _field_from_prop(name: str, prop: dict[str, Any], required: set[str]) -> dict[str, Any]:
        ftype = _map_type(prop)
        select_record = None
        ref = _ref_name(prop.get("$ref"))
        if ref and ref.lower() not in _SKIP_SCHEMAS and "-" not in ref:
            select_record = ref.lower()
        for key in ("x-ns-target-record", "x-ns-record-type", "x-ns-select-record"):
            if prop.get(key):
                select_record = str(prop[key]).lower()
        enum_values = prop.get("enum")
        if enum_values is None and isinstance(prop.get("x-ns-enum-values"), list):
            enum_values = prop["x-ns-enum-values"]
        help_text = prop.get("description")
        nullable = prop.get("nullable", True)
        return {
            "field_id": name,
            "label": prop.get("title") or _humanize(name),
            "type": ftype,
            "required": name.lower() in required or (nullable is False and not prop.get("readOnly")),
            "read_only": bool(prop.get("readOnly", False)),
            "is_custom": bool(prop.get("x-ns-custom-field", False)) or is_custom_field(name),
            "select_record": select_record,
            "enum_values": enum_values,
            "max_length": prop.get("maxLength"),
            "help": help_text,
        }


def _humanize(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ")
    return s[:1].upper() + s[1:]
