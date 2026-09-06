"""Structured NetSuite record/field catalog backed by PostgreSQL.

This is the agent's precise data-model knowledge: every record type, field,
sublist and sublist field, with types, required flags, referenced records and
help text. It is populated from NetSuite's own metadata (REST metadata catalog,
Records Browser) rather than hand-written.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg2.extras import execute_values

from knowledge_base import db

logger = logging.getLogger(__name__)

SHARED = ""  # tenant_id value for standard/out-of-the-box knowledge

_CUSTOM_FIELD_PREFIXES = (
    "custbody", "custentity", "custitem", "custrecord", "custcol", "custevent",
    "custpage", "custitemnumber", "custrecord_", "custentity_",
)

_CATEGORY_HINTS: dict[str, str] = {
    # entities
    "customer": "entity", "vendor": "entity", "employee": "entity", "contact": "entity",
    "partner": "entity", "lead": "entity", "prospect": "entity", "job": "entity",
    "project": "entity", "othername": "entity",
    # transactions
    "salesorder": "transaction", "invoice": "transaction", "cashsale": "transaction",
    "estimate": "transaction", "opportunity": "transaction", "purchaseorder": "transaction",
    "vendorbill": "transaction", "vendorcredit": "transaction", "vendorpayment": "transaction",
    "customerpayment": "transaction", "customerdeposit": "transaction",
    "creditmemo": "transaction", "journalentry": "transaction", "itemfulfillment": "transaction",
    "itemreceipt": "transaction", "returnauthorization": "transaction",
    "vendorreturnauthorization": "transaction", "transferorder": "transaction",
    "inventoryadjustment": "transaction", "inventorytransfer": "transaction",
    "deposit": "transaction", "check": "transaction", "expensereport": "transaction",
    "workorder": "transaction", "assemblybuild": "transaction", "assemblyunbuild": "transaction",
    "intercompanyjournalentry": "transaction", "advintercompanyjournalentry": "transaction",
    "revenuearrangement": "transaction", "depositapplication": "transaction",
    # lists / setup
    "account": "list", "subsidiary": "list", "department": "list", "classification": "list",
    "location": "list", "currency": "list", "term": "list", "paymentmethod": "list",
    "pricelevel": "list", "taxgroup": "list", "salestaxitem": "list", "customercategory": "list",
    "vendorcategory": "list", "itemcategory": "list", "unitstype": "list", "accountingperiod": "list",
    "billingschedule": "list", "campaign": "list", "promotioncode": "list",
    # support / activities
    "supportcase": "support", "issue": "support", "task": "activity", "phonecall": "activity",
    "calendarevent": "activity", "message": "activity", "note": "activity",
}


def is_custom_field(field_id: str) -> bool:
    fid = field_id.lower()
    return fid.startswith(_CUSTOM_FIELD_PREFIXES)


def infer_category(record_id: str) -> str:
    rid = record_id.lower()
    if rid in _CATEGORY_HINTS:
        return _CATEGORY_HINTS[rid]
    if rid.startswith("customrecord") or rid.startswith("customlist") or rid.startswith("customtransaction"):
        return "custom"
    if rid.endswith("item") or rid in ("kititem", "assemblyitem", "serviceitem", "noninventoryitem"):
        return "item"
    if rid.endswith("order") or rid.endswith("entry") or rid.endswith("payment") or rid.endswith("bill"):
        return "transaction"
    return "other"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class NetSuiteCatalog:
    """Read/write access to the ns_* tables."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_record_type(
        self,
        record_id: str,
        *,
        label: str | None = None,
        category: str | None = None,
        description: str | None = None,
        url: str | None = None,
        source: str,
        version: str | None = None,
        tenant_id: str = SHARED,
    ) -> None:
        record_id = record_id.lower().strip()
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ns_record_types
                    (id, tenant_id, label, category, description, url, source, version, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id, tenant_id) DO UPDATE SET
                    label       = COALESCE(EXCLUDED.label, ns_record_types.label),
                    category    = COALESCE(EXCLUDED.category, ns_record_types.category),
                    description = COALESCE(EXCLUDED.description, ns_record_types.description),
                    url         = COALESCE(EXCLUDED.url, ns_record_types.url),
                    source      = EXCLUDED.source,
                    version     = COALESCE(EXCLUDED.version, ns_record_types.version),
                    updated_at  = NOW()
                """,
                (
                    record_id, tenant_id, label, category or infer_category(record_id),
                    description, url, source, version,
                ),
            )

    def upsert_fields(
        self,
        record_id: str,
        fields: list[dict[str, Any]],
        *,
        source: str,
        tenant_id: str = SHARED,
    ) -> int:
        """fields: [{field_id, label, type, required, read_only, select_record,
        enum_values, max_length, help}]"""
        if not fields:
            return 0
        record_id = record_id.lower().strip()
        rows = []
        for f in fields:
            fid = (f.get("field_id") or "").strip()
            if not fid:
                continue
            enum_values = f.get("enum_values")
            rows.append((
                record_id, fid.lower(), tenant_id,
                f.get("label"), f.get("type"),
                bool(f.get("required", False)), bool(f.get("read_only", False)),
                bool(f.get("is_custom", is_custom_field(fid))),
                f.get("select_record"),
                json.dumps(enum_values) if enum_values is not None else None,
                f.get("max_length"), f.get("help"), source,
            ))
        if not rows:
            return 0
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO ns_fields
                    (record_type, field_id, tenant_id, label, type, required, read_only,
                     is_custom, select_record, enum_values, max_length, help, source)
                VALUES %s
                ON CONFLICT (record_type, field_id, tenant_id) DO UPDATE SET
                    label         = COALESCE(EXCLUDED.label, ns_fields.label),
                    type          = COALESCE(EXCLUDED.type, ns_fields.type),
                    required      = ns_fields.required OR EXCLUDED.required,
                    read_only     = EXCLUDED.read_only,
                    is_custom     = EXCLUDED.is_custom,
                    select_record = COALESCE(EXCLUDED.select_record, ns_fields.select_record),
                    enum_values   = COALESCE(EXCLUDED.enum_values, ns_fields.enum_values),
                    max_length    = COALESCE(EXCLUDED.max_length, ns_fields.max_length),
                    help          = COALESCE(EXCLUDED.help, ns_fields.help),
                    source        = EXCLUDED.source,
                    updated_at    = NOW()
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                page_size=500,
            )
        return len(rows)

    def upsert_sublist(
        self,
        record_id: str,
        sublist_id: str,
        *,
        label: str | None,
        fields: list[dict[str, Any]],
        source: str,
        tenant_id: str = SHARED,
    ) -> int:
        record_id = record_id.lower().strip()
        sublist_id = sublist_id.lower().strip()
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ns_sublists (record_type, sublist_id, tenant_id, label, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (record_type, sublist_id, tenant_id) DO UPDATE SET
                    label = COALESCE(EXCLUDED.label, ns_sublists.label),
                    source = EXCLUDED.source
                """,
                (record_id, sublist_id, tenant_id, label, source),
            )
            rows = [
                (
                    record_id, sublist_id, (f.get("field_id") or "").lower().strip(), tenant_id,
                    f.get("label"), f.get("type"), bool(f.get("required", False)),
                    bool(f.get("is_custom", is_custom_field(f.get("field_id") or ""))),
                    f.get("select_record"), f.get("help"), source,
                )
                for f in fields
                if (f.get("field_id") or "").strip()
            ]
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO ns_sublist_fields
                        (record_type, sublist_id, field_id, tenant_id, label, type, required,
                         is_custom, select_record, help, source)
                    VALUES %s
                    ON CONFLICT (record_type, sublist_id, field_id, tenant_id) DO UPDATE SET
                        label         = COALESCE(EXCLUDED.label, ns_sublist_fields.label),
                        type          = COALESCE(EXCLUDED.type, ns_sublist_fields.type),
                        required      = ns_sublist_fields.required OR EXCLUDED.required,
                        is_custom     = EXCLUDED.is_custom,
                        select_record = COALESCE(EXCLUDED.select_record, ns_sublist_fields.select_record),
                        help          = COALESCE(EXCLUDED.help, ns_sublist_fields.help),
                        source        = EXCLUDED.source
                    """,
                    rows,
                    page_size=500,
                )
        return len(rows)

    def purge_source(self, source: str, tenant_id: str = SHARED) -> dict[str, int]:
        """Delete everything a given source contributed, so a re-import is a
        refresh rather than an accumulation of stale rows (renamed sublists,
        record types NetSuite has dropped). Other sources are untouched."""
        removed: dict[str, int] = {}
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            for table in ("ns_sublist_fields", "ns_sublists", "ns_fields", "ns_record_types"):
                cur.execute(
                    f"DELETE FROM {table} WHERE source = %s AND tenant_id = %s", (source, tenant_id)
                )
                removed[table] = cur.rowcount
        logger.info("Purged source '%s': %s", source, removed)
        return removed

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_record_types(
        self, category: str | None = None, tenant_id: str = SHARED
    ) -> list[dict[str, Any]]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            sql = """
                SELECT r.id, r.label, r.category, r.description, r.tenant_id,
                       (SELECT COUNT(*) FROM ns_fields f
                         WHERE f.record_type = r.id AND f.tenant_id IN ('', %s)) AS field_count
                FROM ns_record_types r
                WHERE r.tenant_id IN ('', %s)
            """
            params: list[Any] = [tenant_id, tenant_id]
            if category:
                sql += " AND r.category = %s"
                params.append(category)
            sql += " ORDER BY r.category, r.id"
            cur.execute(sql, params)
            return [
                {"id": r[0], "label": r[1], "category": r[2], "description": r[3],
                 "tenant_id": r[4], "field_count": r[5]}
                for r in cur.fetchall()
            ]

    def resolve_record_type(self, name: str, tenant_id: str = SHARED) -> str | None:
        """Map a user-supplied name ('Sales Order', 'salesorders', 'SO') to a catalog id."""
        if not name:
            return None
        key = _norm(name)
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, label FROM ns_record_types WHERE tenant_id IN ('', %s)", (tenant_id,)
            )
            rows = cur.fetchall()
        if not rows:
            return None
        by_id = {r[0]: r[0] for r in rows}
        if key in by_id:
            return key
        for rid, label in rows:
            if label and _norm(label) == key:
                return rid
        # plural / minor variations
        for candidate in (key.rstrip("s"), key + "s"):
            if candidate in by_id:
                return candidate
        for rid, label in rows:
            if label and _norm(label).rstrip("s") == key.rstrip("s"):
                return rid
        # Fall back to the hand-written alias table
        try:
            from netsuite.field_registry import resolve_record_type as legacy_resolve
            legacy = legacy_resolve(name)
            if legacy and legacy in by_id:
                return legacy
        except Exception:
            pass
        return None

    def get_record(
        self, record_id: str, tenant_id: str = SHARED, include_sublists: bool = True
    ) -> dict[str, Any] | None:
        record_id = record_id.lower().strip()
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, category, description, url, source, version
                FROM ns_record_types WHERE id = %s AND tenant_id IN ('', %s)
                ORDER BY tenant_id DESC LIMIT 1
                """,
                (record_id, tenant_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            record = {
                "id": row[0], "label": row[1], "category": row[2], "description": row[3],
                "url": row[4], "source": row[5], "version": row[6], "fields": [], "sublists": [],
            }
            cur.execute(
                """
                SELECT field_id, label, type, required, read_only, is_custom,
                       select_record, enum_values, max_length, help, tenant_id
                FROM ns_fields WHERE record_type = %s AND tenant_id IN ('', %s)
                ORDER BY required DESC, is_custom, field_id
                """,
                (record_id, tenant_id),
            )
            record["fields"] = [self._field_row(r) for r in cur.fetchall()]
            if include_sublists:
                cur.execute(
                    """
                    SELECT sublist_id, label FROM ns_sublists
                    WHERE record_type = %s AND tenant_id IN ('', %s) ORDER BY sublist_id
                    """,
                    (record_id, tenant_id),
                )
                sublists = [{"id": r[0], "label": r[1], "fields": []} for r in cur.fetchall()]
                for sl in sublists:
                    cur.execute(
                        """
                        SELECT field_id, label, type, required, is_custom, select_record, help
                        FROM ns_sublist_fields
                        WHERE record_type = %s AND sublist_id = %s AND tenant_id IN ('', %s)
                        ORDER BY required DESC, field_id
                        """,
                        (record_id, sl["id"], tenant_id),
                    )
                    sl["fields"] = [
                        {"field_id": r[0], "label": r[1], "type": r[2], "required": r[3],
                         "is_custom": r[4], "select_record": r[5], "help": r[6]}
                        for r in cur.fetchall()
                    ]
                record["sublists"] = sublists
        return record

    def get_field(
        self, record_id: str, field_id: str, tenant_id: str = SHARED
    ) -> dict[str, Any] | None:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT field_id, label, type, required, read_only, is_custom,
                       select_record, enum_values, max_length, help, tenant_id
                FROM ns_fields
                WHERE record_type = %s AND field_id = %s AND tenant_id IN ('', %s)
                ORDER BY tenant_id DESC LIMIT 1
                """,
                (record_id.lower().strip(), field_id.lower().strip(), tenant_id),
            )
            row = cur.fetchone()
        return self._field_row(row) if row else None

    def find_fields(
        self,
        keyword: str,
        record_id: str | None = None,
        limit: int = 30,
        tenant_id: str = SHARED,
    ) -> list[dict[str, Any]]:
        """Keyword search across field ids, labels and help text."""
        pattern = f"%{keyword.strip()}%"
        params: list[Any] = [tenant_id, pattern, pattern, pattern]
        sql = """
            SELECT record_type, field_id, label, type, required, is_custom, select_record, help
            FROM ns_fields
            WHERE tenant_id IN ('', %s)
              AND (field_id ILIKE %s OR label ILIKE %s OR help ILIKE %s)
        """
        if record_id:
            sql += " AND record_type = %s"
            params.append(record_id.lower().strip())
        sql += """
            ORDER BY (field_id ILIKE %s) DESC, (label ILIKE %s) DESC, record_type, field_id
            LIMIT %s
        """
        params += [pattern, pattern, limit]
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {"record_type": r[0], "field_id": r[1], "label": r[2], "type": r[3],
             "required": r[4], "is_custom": r[5], "select_record": r[6], "help": r[7]}
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        with db.connection(register_vector_type=False) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ns_record_types")
            records = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_custom) FROM ns_fields")
            fields, custom = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM ns_sublists")
            sublists = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ns_sublist_fields")
            sublist_fields = cur.fetchone()[0]
            cur.execute("SELECT category, COUNT(*) FROM ns_record_types GROUP BY category ORDER BY 1")
            by_category = {r[0] or "other": r[1] for r in cur.fetchall()}
            cur.execute("SELECT source, COUNT(*) FROM ns_record_types GROUP BY source")
            by_source = {r[0] or "unknown": r[1] for r in cur.fetchall()}
        return {
            "record_types": records,
            "fields": fields,
            "custom_fields": custom,
            "sublists": sublists,
            "sublist_fields": sublist_fields,
            "by_category": by_category,
            "by_source": by_source,
        }

    # ------------------------------------------------------------------
    # Formatting for the agent
    # ------------------------------------------------------------------

    @staticmethod
    def _field_row(r: tuple) -> dict[str, Any]:
        return {
            "field_id": r[0], "label": r[1], "type": r[2], "required": r[3],
            "read_only": r[4], "is_custom": r[5], "select_record": r[6],
            "enum_values": r[7], "max_length": r[8], "help": r[9], "tenant_id": r[10],
        }

    @staticmethod
    def format_field(record_id: str, f: dict[str, Any]) -> str:
        lines = [f"{record_id}.{f['field_id']}"]
        if f.get("label"):
            lines.append(f"  Label     : {f['label']}")
        if f.get("type"):
            lines.append(f"  Type      : {f['type']}")
        lines.append(f"  Required  : {'yes' if f.get('required') else 'no'}")
        if f.get("read_only"):
            lines.append("  Read-only : yes")
        if f.get("is_custom"):
            lines.append("  Custom    : yes")
        if f.get("select_record"):
            lines.append(f"  References: {f['select_record']}")
        if f.get("enum_values"):
            vals = f["enum_values"]
            if isinstance(vals, list):
                lines.append(f"  Values    : {', '.join(str(v) for v in vals[:25])}")
        if f.get("max_length"):
            lines.append(f"  Max length: {f['max_length']}")
        if f.get("help"):
            lines.append(f"  Help      : {f['help'][:600]}")
        return "\n".join(lines)

    @staticmethod
    def format_record(record: dict[str, Any], max_fields: int = 80) -> str:
        head = f"NetSuite record: {record['id']}"
        if record.get("label"):
            head += f" ({record['label']})"
        lines = [head]
        if record.get("category"):
            lines.append(f"Category: {record['category']}")
        if record.get("description"):
            lines.append(record["description"][:500])
        fields = record.get("fields", [])
        required = [f for f in fields if f.get("required")]
        lines.append(f"\nFields: {len(fields)} total, {len(required)} required")
        if required:
            lines.append("Required:")
            for f in required:
                lines.append(f"  {f['field_id']} ({f.get('type') or '?'}) — {f.get('label') or ''}")
        others = [f for f in fields if not f.get("required")]
        if others:
            lines.append("Other fields:")
            for f in others[:max_fields]:
                ref = f" -> {f['select_record']}" if f.get("select_record") else ""
                lines.append(f"  {f['field_id']} ({f.get('type') or '?'}{ref}) — {f.get('label') or ''}")
            if len(others) > max_fields:
                lines.append(f"  ... and {len(others) - max_fields} more (ask for a specific field)")
        if record.get("sublists"):
            lines.append("\nSublists:")
            for sl in record["sublists"]:
                fids = ", ".join(f["field_id"] for f in sl["fields"][:20])
                more = f" (+{len(sl['fields']) - 20})" if len(sl["fields"]) > 20 else ""
                lines.append(f"  {sl['id']}: {fids}{more}")
        return "\n".join(lines)
