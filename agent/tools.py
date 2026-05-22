"""Tool definitions and implementations for the NS Migration Agent.

Tool functions receive structured input dicts and return string results.
The TOOL_DEFINITIONS list contains Anthropic-format tool specs for client.messages.create().
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the knowledge base for relevant NetSuite documentation, migration guides, "
            "and best practices. Use this tool FIRST before answering any question about NetSuite "
            "configuration, processes, or field definitions to find relevant uploaded documentation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific — e.g. 'NetSuite customer CSV import required fields' or 'inventory item costing method setup'.",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_netsuite_field_info",
        "description": (
            "Look up field definitions from the NetSuite field registry. Returns field type, "
            "required status, max length, and migration notes for fields on a given record type. "
            "Use when discussing specific NetSuite fields, building mappings, or answering questions about field requirements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "description": "NetSuite record type: customer, vendor, inventoryitem, invoice, employee, journalentry, salesorder, contact",
                },
                "field_id": {
                    "type": "string",
                    "description": "Specific field internal ID to look up (e.g. 'companyname', 'trandate'). If omitted, returns all fields for the record type.",
                },
            },
            "required": ["record_type"],
        },
    },
    {
        "name": "validate_csv_file",
        "description": (
            "Validate a CSV file against a NetSuite record type's field rules. "
            "Checks for required fields, data type correctness, date formats, boolean values, "
            "max lengths, and duplicate externalids. Returns detailed error and warning reports."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the CSV file to validate.",
                },
                "record_type": {
                    "type": "string",
                    "description": "NetSuite record type to validate against: customer, vendor, inventoryitem, invoice, employee, journalentry, salesorder, contact",
                },
            },
            "required": ["file_path", "record_type"],
        },
    },
    {
        "name": "map_fields",
        "description": (
            "Suggest field mappings from source system fields to NetSuite fields for a given record type. "
            "Uses exact matching, source-system-specific knowledge (SAP, Salesforce, QuickBooks, Dynamics), "
            "and fuzzy matching to suggest the best mapping with confidence scores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of source system field/column names to map.",
                },
                "source_system": {
                    "type": "string",
                    "description": "Source system name for system-specific hints: salesforce, quickbooks, sap, dynamics, or generic.",
                    "default": "generic",
                },
                "target_record_type": {
                    "type": "string",
                    "description": "Target NetSuite record type: customer, vendor, inventoryitem, invoice, employee, journalentry, salesorder, contact",
                },
            },
            "required": ["source_fields", "target_record_type"],
        },
    },
    {
        "name": "transform_data",
        "description": (
            "Apply a field mapping to transform a source CSV file into NetSuite-ready format. "
            "Normalizes dates to MM/DD/YYYY, strips currency symbols, normalizes booleans, "
            "formats phone numbers, and cleans text values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file_path": {
                    "type": "string",
                    "description": "Path to the source CSV file.",
                },
                "record_type": {
                    "type": "string",
                    "description": "Target NetSuite record type.",
                },
                "mapping": {
                    "type": "object",
                    "description": "Field mapping dict: {source_column: netsuite_field_id}. Use null value to drop a column.",
                    "additionalProperties": {"type": ["string", "null"]},
                },
                "output_path": {
                    "type": "string",
                    "description": "Path to write the transformed CSV. If omitted, writes to same directory with '_netsuite_import.csv' suffix.",
                },
            },
            "required": ["source_file_path", "record_type", "mapping"],
        },
    },
    {
        "name": "generate_import_file",
        "description": (
            "Generate a NetSuite CSV Import-ready file from a source CSV using auto-suggested field mappings. "
            "Combines field mapping suggestion with full data transformation in one step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file_path": {
                    "type": "string",
                    "description": "Path to the source CSV file.",
                },
                "record_type": {
                    "type": "string",
                    "description": "Target NetSuite record type.",
                },
                "source_system": {
                    "type": "string",
                    "description": "Source system for mapping hints (salesforce, quickbooks, sap, dynamics, generic).",
                    "default": "generic",
                },
                "output_path": {
                    "type": "string",
                    "description": "Output file path. Defaults to source file name + '_netsuite_import.csv'.",
                },
            },
            "required": ["source_file_path", "record_type"],
        },
    },
    {
        "name": "get_project_context",
        "description": (
            "Retrieve project-specific notes, field mapping decisions, and migration history for the current project. "
            "Use this at the start of a conversation or when the user references previous decisions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to retrieve context for.",
                },
                "note_type": {
                    "type": "string",
                    "description": "Filter by note type: field_mapping, validation_rule, general, decision. Omit for all notes.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "save_project_note",
        "description": (
            "Save an important note, field mapping decision, or migration choice to the project memory. "
            "Use this to persist decisions so they are available in future conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to save the note to.",
                },
                "note_type": {
                    "type": "string",
                    "description": "Type of note: field_mapping, validation_rule, general, decision, issue",
                },
                "content": {
                    "type": "string",
                    "description": "The note content to save. Be specific and include field names, values, and rationale.",
                },
            },
            "required": ["project_id", "note_type", "content"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def run_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    memory: Any,
    knowledge_index: Any,
) -> str:
    """Dispatch a tool call and return the string result."""
    try:
        if tool_name == "search_knowledge_base":
            return _search_knowledge_base(tool_input, knowledge_index)
        elif tool_name == "get_netsuite_field_info":
            return _get_netsuite_field_info(tool_input)
        elif tool_name == "validate_csv_file":
            return _validate_csv_file(tool_input)
        elif tool_name == "map_fields":
            return _map_fields(tool_input)
        elif tool_name == "transform_data":
            return _transform_data(tool_input)
        elif tool_name == "generate_import_file":
            return _generate_import_file(tool_input)
        elif tool_name == "get_project_context":
            return _get_project_context(tool_input, memory)
        elif tool_name == "save_project_note":
            return _save_project_note(tool_input, memory)
        else:
            return f"Error: Unknown tool '{tool_name}'."
    except Exception as exc:
        logger.exception("Tool '%s' raised an exception.", tool_name)
        return f"Error executing tool '{tool_name}': {exc}"


def _search_knowledge_base(tool_input: dict[str, Any], knowledge_index: Any) -> str:
    query = tool_input.get("query", "")
    n_results = min(int(tool_input.get("n_results", 5)), 10)

    if not query.strip():
        return "Error: query cannot be empty."

    results = knowledge_index.search(query, n_results=n_results)

    if not results:
        return (
            "No results found in the knowledge base for that query. "
            "The knowledge base may be empty — ask the user to upload relevant documentation first."
        )

    lines = [f"Knowledge base search results for: '{query}'\n"]
    for i, hit in enumerate(results, 1):
        score_pct = f"{hit['score'] * 100:.0f}%"
        lines.append(f"--- Result {i} (relevance: {score_pct}, source: {hit['source']}) ---")
        lines.append(hit["text"][:1000])  # Truncate very long chunks
        lines.append("")

    return "\n".join(lines)


def _get_netsuite_field_info(tool_input: dict[str, Any]) -> str:
    from netsuite.field_registry import (
        get_field_info,
        format_field_summary,
        resolve_record_type,
        get_required_fields,
    )

    record_type = tool_input.get("record_type", "")
    field_id = tool_input.get("field_id")

    canonical = resolve_record_type(record_type)
    if canonical is None:
        from netsuite.field_registry import get_record_types
        return (
            f"Unknown record type '{record_type}'. "
            f"Valid record types: {', '.join(get_record_types())}"
        )

    if field_id:
        info = get_field_info(canonical, field_id)
        if info is None:
            return f"Field '{field_id}' not found in {canonical} record type."
        return (
            f"NetSuite {canonical}.{field_id}:\n"
            f"  Label     : {info['label']}\n"
            f"  Type      : {info['type']}\n"
            f"  Required  : {info['required']}\n"
            f"  Max Length: {info.get('max_length', 'N/A')}\n"
            f"  Notes     : {info['notes']}"
        )
    else:
        return format_field_summary(canonical)


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

    validator = CSVValidator()
    result = validator.validate(path, record_type)
    return result.summary()


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

    # Also build a JSON mapping dict for easy copy/paste
    mapping_json = {src: info["netsuite_field"] for src, info in result.items()}

    return (
        f"Field Mapping Suggestions ({source_system} → NetSuite {target_record_type}):\n\n"
        f"{table}\n\n"
        f"Mapping as JSON (for use with transform_data tool):\n"
        f"{json.dumps(mapping_json, indent=2)}"
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
            f"Transform complete.\n"
            f"  Source rows : {len(df)}\n"
            f"  Output rows : {len(transformed)}\n"
            f"  Columns     : {', '.join(transformed.columns)}\n"
            f"  Output file : {out}\n\n"
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

    transformer = DataTransformer()
    try:
        df, out = transformer.auto_transform_csv(
            src, record_type, output_path, source_system
        )
        return (
            f"Generated NetSuite import file.\n"
            f"  Record type : {record_type}\n"
            f"  Source      : {source_path}\n"
            f"  Output      : {out}\n"
            f"  Rows        : {len(df)}\n"
            f"  Columns     : {', '.join(df.columns)}\n\n"
            f"Review the mapping and validate before importing into NetSuite."
        )
    except Exception as exc:
        return f"Error generating import file: {exc}"


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
            note_lines = [f"\nFiltered notes (type={note_type}):"]
            for note in notes:
                note_lines.append(f"  [{note['created_at']}] {note['content']}")
            summary += "\n" + "\n".join(note_lines)

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
