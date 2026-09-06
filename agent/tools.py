"""Tool definitions and implementations for the NetSuite expert agent.

Tool functions receive structured input dicts and return string results.
TOOL_DEFINITIONS contains Anthropic-format tool specs for client.messages.create().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODULES = (
    "suitescript, suitetalk, suiteflow, suiteanalytics, suitecloud, csv_import, accounting, "
    "oneworld, order_management, inventory, purchasing, banking, tax, crm, projects, "
    "payroll_hr, ecommerce, administration, reporting"
)

# ---------------------------------------------------------------------------
# Anthropic tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Semantic search over NetSuite documentation (Help Center, guides, uploaded docs) and "
            "learned knowledge. Use for how-to, behaviour, setup steps, limits and gotchas. "
            "For exact field/record facts prefer get_netsuite_field_info or find_netsuite_field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Specific search query, e.g. 'sales order approval workflow setup' or 'CSV import customer required columns'.",
                },
                "n_results": {"type": "integer", "description": "Results to return (default 5, max 10).", "default": 5},
                "module": {
                    "type": "string",
                    "description": f"Optional module filter. One of: {_MODULES}.",
                },
                "doc_type": {
                    "type": "string",
                    "description": "Optional filter: help (Oracle Help Center), upload (uploaded docs), chat_learning, url.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_netsuite_field_info",
        "description": (
            "Authoritative NetSuite data model lookup. Given a record type (e.g. 'salesorder', "
            "'Customer', 'inventory item') returns its fields with internal IDs, types, required flags, "
            "referenced records and sublists. Pass field_id to get one field in detail (also finds "
            "sublist fields). Use this whenever a question involves a record, field, internal ID, "
            "what's required, or what a field references."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {"type": "string", "description": "Record type name or internal ID, e.g. 'salesorder', 'customer', 'Vendor Bill'."},
                "field_id": {"type": "string", "description": "Optional field internal ID, e.g. 'entity', 'trandate', 'custbody_xyz'."},
            },
            "required": ["record_type"],
        },
    },
    {
        "name": "find_netsuite_field",
        "description": (
            "Keyword search across every NetSuite field's internal ID, label and help text, "
            "optionally within one record type. Use when you know roughly what a field is called "
            "but not its internal ID, or to see which records carry a field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "e.g. 'credit limit', 'ship date', 'terms'."},
                "record_type": {"type": "string", "description": "Optional record type to restrict the search."},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "list_netsuite_record_types",
        "description": "List NetSuite record types in the catalog, optionally by category (entity, transaction, item, list, activity, support, custom, other).",
        "input_schema": {
            "type": "object",
            "properties": {"category": {"type": "string", "description": "Optional category filter."}},
        },
    },
    {
        "name": "validate_csv_file",
        "description": (
            "Validate a CSV file against a NetSuite record type's field rules: required fields, types, "
            "date formats, booleans, max lengths, duplicate externalids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the CSV file."},
                "record_type": {"type": "string", "description": "customer, vendor, inventoryitem, invoice, employee, journalentry, salesorder, contact"},
            },
            "required": ["file_path", "record_type"],
        },
    },
    {
        "name": "map_fields",
        "description": (
            "Suggest mappings from source-system fields to NetSuite fields for a record type using exact, "
            "source-specific (SAP, Salesforce, QuickBooks, Dynamics) and fuzzy matching with confidence scores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_fields": {"type": "array", "items": {"type": "string"}, "description": "Source column names."},
                "source_system": {"type": "string", "description": "salesforce, quickbooks, sap, dynamics, or generic.", "default": "generic"},
                "target_record_type": {"type": "string", "description": "customer, vendor, inventoryitem, invoice, employee, journalentry, salesorder, contact"},
            },
            "required": ["source_fields", "target_record_type"],
        },
    },
    {
        "name": "transform_data",
        "description": (
            "Apply a field mapping to transform a source CSV into NetSuite-ready format (dates to MM/DD/YYYY, "
            "currency symbols stripped, booleans normalised, phones formatted)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file_path": {"type": "string"},
                "record_type": {"type": "string"},
                "mapping": {"type": "object", "description": "{source_column: netsuite_field_id}; null drops a column.", "additionalProperties": {"type": ["string", "null"]}},
                "output_path": {"type": "string"},
            },
            "required": ["source_file_path", "record_type", "mapping"],
        },
    },
    {
        "name": "generate_import_file",
        "description": "Generate a NetSuite CSV Import-ready file from a source CSV using auto-suggested mappings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file_path": {"type": "string"},
                "record_type": {"type": "string"},
                "source_system": {"type": "string", "default": "generic"},
                "output_path": {"type": "string"},
            },
            "required": ["source_file_path", "record_type"],
        },
    },
    {
        "name": "get_project_context",
        "description": "Retrieve project notes, field mapping decisions and history for the current project. Use when the user references prior decisions or their own setup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "note_type": {"type": "string", "description": "field_mapping, validation_rule, general, decision, issue. Omit for all."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "save_project_note",
        "description": "Persist an important decision, mapping or fact to project memory for future conversations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "note_type": {"type": "string", "description": "field_mapping, validation_rule, general, decision, issue"},
                "content": {"type": "string", "description": "Specific content: field names, values, rationale."},
            },
            "required": ["project_id", "note_type", "content"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def run_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    memory: Any,
    knowledge_index: Any,
    catalog: Any = None,
    tenant_id: str = "",
) -> str:
    try:
        if tool_name == "search_knowledge_base":
            return _search_knowledge_base(tool_input, knowledge_index, tenant_id)
        if tool_name == "get_netsuite_field_info":
            return _get_netsuite_field_info(tool_input, catalog, tenant_id)
        if tool_name == "find_netsuite_field":
            return _find_netsuite_field(tool_input, catalog, tenant_id)
        if tool_name == "list_netsuite_record_types":
            return _list_record_types(tool_input, catalog, tenant_id)
        if tool_name == "validate_csv_file":
            return _validate_csv_file(tool_input)
        if tool_name == "map_fields":
            return _map_fields(tool_input)
        if tool_name == "transform_data":
            return _transform_data(tool_input)
        if tool_name == "generate_import_file":
            return _generate_import_file(tool_input)
        if tool_name == "get_project_context":
            return _get_project_context(tool_input, memory)
        if tool_name == "save_project_note":
            return _save_project_note(tool_input, memory)
        return f"Error: Unknown tool '{tool_name}'."
    except Exception as exc:
        logger.exception("Tool '%s' raised an exception.", tool_name)
        return f"Error executing tool '{tool_name}': {exc}"


# ---------------------------------------------------------------------------
# Knowledge search
# ---------------------------------------------------------------------------

def _search_knowledge_base(tool_input: dict[str, Any], knowledge_index: Any, tenant_id: str) -> str:
    query = (tool_input.get("query") or "").strip()
    n_results = min(int(tool_input.get("n_results", 5) or 5), 10)
    if not query:
        return "Error: query cannot be empty."

    where = {k: tool_input.get(k) for k in ("module", "doc_type") if tool_input.get(k)}
    results = knowledge_index.search(query, n_results=n_results, where=where or None, tenant_id=tenant_id or None)
    if not results and where:
        results = knowledge_index.search(query, n_results=n_results, tenant_id=tenant_id or None)
    if not results:
        return "No results in the knowledge base for that query."

    lines = [f"Knowledge base results for: '{query}'\n"]
    for i, hit in enumerate(results, 1):
        meta = hit.get("metadata") or {}
        where_from = meta.get("title") or hit["source"]
        extra = " | ".join(x for x in (meta.get("module"), meta.get("doc_type")) if x)
        lines.append(f"--- Result {i} ({hit['score'] * 100:.0f}% | {where_from}{' | ' + extra if extra else ''}) ---")
        if meta.get("url"):
            lines.append(f"URL: {meta['url']}")
        lines.append(hit["text"][:1200])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Structured catalog lookups (with hand-written registry as fallback/enrichment)
# ---------------------------------------------------------------------------

def _legacy_field(record_type: str, field_id: str | None) -> dict[str, Any] | None:
    try:
        from netsuite.field_registry import get_field_info, resolve_record_type

        canonical = resolve_record_type(record_type)
        if not canonical:
            return None
        return get_field_info(canonical, field_id)
    except Exception:
        return None


def _get_netsuite_field_info(tool_input: dict[str, Any], catalog: Any, tenant_id: str) -> str:
    record_type = (tool_input.get("record_type") or "").strip()
    field_id = (tool_input.get("field_id") or "").strip() or None
    if not record_type:
        return "Error: record_type is required."

    record_id = catalog.resolve_record_type(record_type, tenant_id) if catalog else None

    if record_id is None:
        # Catalog empty or unknown record: fall back to the hand-written registry.
        from netsuite.field_registry import format_field_summary, get_record_types, resolve_record_type

        canonical = resolve_record_type(record_type)
        if canonical is None:
            known = ", ".join(r["id"] for r in catalog.list_record_types(tenant_id=tenant_id)[:60]) if catalog else ""
            return (
                f"Unknown record type '{record_type}'."
                + (f" Catalog record types include: {known}" if known else f" Registry record types: {', '.join(get_record_types())}")
            )
        if field_id:
            info = _legacy_field(canonical, field_id)
            if not info:
                return f"Field '{field_id}' not found on {canonical}."
            return (
                f"NetSuite {canonical}.{field_id}:\n  Label: {info['label']}\n  Type: {info['type']}\n"
                f"  Required: {info['required']}\n  Max Length: {info.get('max_length', 'N/A')}\n  Notes: {info['notes']}"
            )
        return format_field_summary(canonical)

    if field_id:
        f = catalog.get_field(record_id, field_id, tenant_id)
        out = ""
        if f:
            out = catalog.format_field(record_id, f)
        else:
            # Look through sublists
            record = catalog.get_record(record_id, tenant_id, include_sublists=True)
            for sl in (record or {}).get("sublists", []):
                for sf in sl["fields"]:
                    if sf["field_id"] == field_id.lower():
                        out = f"{record_id}.{sl['id']}.{sf['field_id']} (sublist '{sl['id']}')\n" + "\n".join(
                            f"  {k.capitalize():<10}: {v}" for k, v in sf.items() if v not in (None, False, "") and k != "field_id"
                        )
                        break
                if out:
                    break
        if not out:
            similar = catalog.find_fields(field_id, record_id, limit=8, tenant_id=tenant_id)
            hint = ", ".join(s["field_id"] for s in similar) if similar else "none"
            return f"Field '{field_id}' not found on {record_id}. Similar fields: {hint}"
        legacy = _legacy_field(record_id, field_id)
        if legacy and legacy.get("notes"):
            out += f"\n  Migration notes: {legacy['notes']}"
        return out

    record = catalog.get_record(record_id, tenant_id)
    if not record:
        return f"Record type '{record_id}' has no catalog entry yet."
    text = catalog.format_record(record)
    legacy_all = _legacy_field(record_id, None)
    if legacy_all:
        notes = [f"  {fid}: {d['notes']}" for fid, d in legacy_all.items() if d.get("notes")]
        if notes:
            text += "\n\nMigration notes (hand-curated):\n" + "\n".join(notes[:25])
    return text


def _find_netsuite_field(tool_input: dict[str, Any], catalog: Any, tenant_id: str) -> str:
    keyword = (tool_input.get("keyword") or "").strip()
    if not keyword:
        return "Error: keyword is required."
    if not catalog:
        return "Catalog not available."
    record_id = None
    if tool_input.get("record_type"):
        record_id = catalog.resolve_record_type(tool_input["record_type"], tenant_id)
    hits = catalog.find_fields(keyword, record_id, limit=30, tenant_id=tenant_id)
    if not hits:
        return f"No fields matching '{keyword}'" + (f" on {record_id}" if record_id else "") + "."
    lines = [f"Fields matching '{keyword}':"]
    for h in hits:
        req = " [required]" if h.get("required") else ""
        ref = f" -> {h['select_record']}" if h.get("select_record") else ""
        custom = " [custom]" if h.get("is_custom") else ""
        lines.append(f"  {h['record_type']}.{h['field_id']} ({h.get('type') or '?'}{ref}){req}{custom} — {h.get('label') or ''}")
        if h.get("help"):
            lines.append(f"      {h['help'][:160]}")
    return "\n".join(lines)


def _list_record_types(tool_input: dict[str, Any], catalog: Any, tenant_id: str) -> str:
    if not catalog:
        return "Catalog not available."
    category = (tool_input.get("category") or "").strip() or None
    records = catalog.list_record_types(category, tenant_id)
    if not records:
        return "The catalog has no record types yet." + (f" (category={category})" if category else "")
    by_cat: dict[str, list[str]] = {}
    for r in records:
        by_cat.setdefault(r.get("category") or "other", []).append(
            f"{r['id']}" + (f" ({r['label']})" if r.get("label") and r["label"].lower().replace(' ', '') != r["id"] else "")
        )
    lines = [f"{len(records)} record types:"]
    for cat, ids in by_cat.items():
        lines.append(f"{cat}: " + ", ".join(ids))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data tools (unchanged behaviour)
# ---------------------------------------------------------------------------

def _validate_csv_file(tool_input: dict[str, Any]) -> str:
    from data_tools.validator import CSVValidator

    file_path = tool_input.get("file_path", "")
    record_type = tool_input.get("record_type", "")
    if not file_path:
        return "Error: file_path is required."
    if not record_type:
        return "Error: record_type is required."
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"
    return CSVValidator().validate(path, record_type).summary()


def _map_fields(tool_input: dict[str, Any]) -> str:
    from data_tools.mapper import FieldMapper

    source_fields = tool_input.get("source_fields", [])
    source_system = tool_input.get("source_system", "generic")
    target_record_type = tool_input.get("target_record_type", "customer")
    if not source_fields:
        return "Error: source_fields list is required."
    mapper = FieldMapper()
    result = mapper.suggest_mapping(source_fields, source_system, target_record_type)
    table = mapper.format_mapping_table(result)
    mapping_json = {src: info["netsuite_field"] for src, info in result.items()}
    return (
        f"Field Mapping Suggestions ({source_system} → NetSuite {target_record_type}):\n\n{table}\n\n"
        f"Mapping as JSON (for transform_data):\n{json.dumps(mapping_json, indent=2)}"
    )


def _transform_data(tool_input: dict[str, Any]) -> str:
    import pandas as pd
    from data_tools.transformer import DataTransformer

    source_path = tool_input.get("source_file_path", "")
    record_type = tool_input.get("record_type", "")
    mapping = tool_input.get("mapping", {})
    output_path = tool_input.get("output_path")
    if not source_path:
        return "Error: source_file_path is required."
    src = Path(source_path)
    if not src.exists():
        return f"Error: Source file not found: {source_path}"
    if output_path is None:
        output_path = str(src.parent / f"{src.stem}_netsuite_import.csv")
    try:
        df = pd.read_csv(str(src), dtype=str).fillna("")
    except Exception as exc:
        return f"Error reading source CSV: {exc}"
    transformer = DataTransformer()
    try:
        transformed = transformer.transform(df, mapping, record_type)
        out = transformer.generate_netsuite_csv(transformed, record_type, output_path)
        return (
            f"Transform complete.\n  Source rows : {len(df)}\n  Output rows : {len(transformed)}\n"
            f"  Columns     : {', '.join(transformed.columns)}\n  Output file : {out}\n\n"
            f"The file is ready for NetSuite CSV Import Assistant."
        )
    except Exception as exc:
        return f"Error during transformation: {exc}"


def _generate_import_file(tool_input: dict[str, Any]) -> str:
    from data_tools.transformer import DataTransformer

    source_path = tool_input.get("source_file_path", "")
    record_type = tool_input.get("record_type", "")
    source_system = tool_input.get("source_system", "generic")
    output_path = tool_input.get("output_path")
    if not source_path:
        return "Error: source_file_path is required."
    src = Path(source_path)
    if not src.exists():
        return f"Error: Source file not found: {source_path}"
    if output_path is None:
        output_path = str(src.parent / f"{src.stem}_netsuite_import.csv")
    try:
        df, out = DataTransformer().auto_transform_csv(src, record_type, output_path, source_system)
        return (
            f"Generated NetSuite import file.\n  Record type : {record_type}\n  Source      : {source_path}\n"
            f"  Output      : {out}\n  Rows        : {len(df)}\n  Columns     : {', '.join(df.columns)}\n\n"
            f"Review the mapping and validate before importing into NetSuite."
        )
    except Exception as exc:
        return f"Error generating import file: {exc}"


# ---------------------------------------------------------------------------
# Project memory
# ---------------------------------------------------------------------------

def _get_project_context(tool_input: dict[str, Any], memory: Any) -> str:
    project_id = tool_input.get("project_id", "")
    note_type = tool_input.get("note_type")
    if not project_id:
        return "Error: project_id is required."
    project = memory.get_project(project_id)
    if project is None:
        return f"Project '{project_id}' not found."
    summary = memory.get_project_summary(project_id)
    if note_type:
        notes = memory.get_project_notes(project_id, note_type)
        if notes:
            summary += f"\n\nFiltered notes (type={note_type}):\n" + "\n".join(
                f"  [{n['created_at']}] {n['content']}" for n in notes
            )
    return summary


def _save_project_note(tool_input: dict[str, Any], memory: Any) -> str:
    project_id = tool_input.get("project_id", "")
    note_type = tool_input.get("note_type", "general")
    content = tool_input.get("content", "")
    if not project_id:
        return "Error: project_id is required."
    if not content:
        return "Error: content is required."
    project = memory.get_project(project_id)
    if project is None:
        return f"Project '{project_id}' not found. Create the project first."
    note_id = memory.save_project_note(project_id, note_type, content)
    return f"Note saved to project '{project['name']}' (ID: {note_id}, type: {note_type})."
