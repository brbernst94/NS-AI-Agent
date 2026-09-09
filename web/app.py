"""Streamlit web UI for NS-AI-Agent: chat, knowledge management, projects, and data tools."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE = os.getenv("NS_AGENT_API", "http://localhost:8000")
_REQUEST_TIMEOUT = 120  # seconds
# Sent when the API has API_ACCESS_KEY set; harmless when it does not.
_HEADERS = {"X-API-Key": os.getenv("NS_AGENT_API_KEY", "")}

st.set_page_config(
    page_title="NS-AI-Agent | NetSuite Migration Assistant",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _parse_response(resp: requests.Response) -> dict | list | None:
    """Parse JSON response, showing a clean error if the body isn't JSON."""
    try:
        return resp.json()
    except Exception:
        status = resp.status_code
        preview = resp.text[:200] if resp.text else "(empty response)"
        st.error(f"API returned non-JSON response (HTTP {status}): {preview}")
        return None


def api_get(path: str, params: dict | None = None) -> dict | list | None:
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        return _parse_response(resp)
    except requests.exceptions.ConnectionError:
        st.error(f"Cannot connect to API at {API_BASE}. Is the server running?")
        return None
    except requests.exceptions.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        st.error(f"API error {exc.response.status_code}: {detail or exc}")
        return None
    except Exception as exc:
        st.error(f"API error: {exc}")
        return None


def api_post(path: str, json_data: dict | None = None, files: dict | None = None) -> dict | None:
    try:
        if files:
            resp = requests.post(f"{API_BASE}{path}", files=files, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        else:
            resp = requests.post(
                f"{API_BASE}{path}", json=json_data, timeout=_REQUEST_TIMEOUT, headers=_HEADERS
            )
        resp.raise_for_status()
        return _parse_response(resp)
    except requests.exceptions.ConnectionError:
        st.error(f"Cannot connect to API at {API_BASE}. Is the server running?")
        return None
    except requests.exceptions.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        st.error(f"API error {exc.response.status_code}: {detail or exc}")
        return None
    except Exception as exc:
        st.error(f"API error: {exc}")
        return None


def api_patch(path: str, json_data: dict) -> dict | None:
    try:
        resp = requests.patch(f"{API_BASE}{path}", json=json_data, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        return _parse_response(resp)
    except Exception as exc:
        st.error(f"API error: {exc}")
        return None


def check_api_health() -> bool:
    try:
        resp = requests.get(f"{API_BASE}/health", timeout=5, headers=_HEADERS)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------

def init_session_state() -> None:
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    if "session_id" not in st.session_state:
        st.session_state.session_id = None
    if "project_id" not in st.session_state:
        st.session_state.project_id = None
    if "projects_cache" not in st.session_state:
        st.session_state.projects_cache = []


init_session_state()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        st.title("NS-AI-Agent")
        st.caption("NetSuite Migration Assistant")

        # API Status
        is_healthy = check_api_health()
        if is_healthy:
            st.success("API Connected")
        else:
            st.error("API Offline")
            st.caption(f"Start server: `uvicorn api.main:app --reload`")

        st.divider()

        # Project selection
        st.subheader("Active Project")
        projects = api_get("/projects")
        if projects:
            st.session_state.projects_cache = projects
            project_options = {"(No project)": None}
            for p in projects:
                if not isinstance(p, dict):
                    continue
                project_options[f"{p['name']} ({p['id'][:8]}...)"] = p["id"]

            current_display = "(No project)"
            if st.session_state.project_id:
                for k, v in project_options.items():
                    if v == st.session_state.project_id:
                        current_display = k
                        break

            selected = st.selectbox(
                "Select project",
                options=list(project_options.keys()),
                index=list(project_options.keys()).index(current_display),
                label_visibility="collapsed",
            )
            new_project_id = project_options[selected]
            if new_project_id != st.session_state.project_id:
                st.session_state.project_id = new_project_id
                st.session_state.session_id = None
                st.session_state.chat_messages = []
                st.rerun()
        else:
            st.caption("No projects yet. Create one in the Projects tab.")

        st.divider()

        # Knowledge base stats
        st.subheader("Knowledge Base")
        stats = api_get("/knowledge/stats")
        if stats:
            st.metric("Total Chunks", stats.get("total_documents", 0))
            if stats.get("sources"):
                with st.expander("Sources"):
                    for src, count in sorted(stats["sources"].items(), key=lambda x: -x[1])[:10]:
                        st.caption(f"{count} chunks — {src[:40]}")

        st.divider()
        st.caption("NS-AI-Agent v1.0")


# ---------------------------------------------------------------------------
# Chat Tab
# ---------------------------------------------------------------------------

def render_chat_tab() -> None:
    st.header("Chat with NetSuite Migration SME")

    if st.session_state.project_id:
        st.info(f"Project context loaded: `{st.session_state.project_id[:16]}...`")

    # Display chat history
    chat_container = st.container()
    with chat_container:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("tool_calls"):
                    st.caption(f"Tools used: {', '.join(msg['tool_calls'])}")

    # Chat input
    if user_input := st.chat_input("Ask about NetSuite migration, field mappings, validation..."):
        # Show user message immediately
        st.session_state.chat_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        # Get agent response
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                result = api_post(
                    "/chat",
                    json_data={
                        "message": user_input,
                        "session_id": st.session_state.session_id,
                        "project_id": st.session_state.project_id,
                    },
                )

            if result:
                st.session_state.session_id = result.get("session_id")
                response_text = result.get("response", "No response received.")

                st.markdown(response_text)

                st.session_state.chat_messages.append(
                    {
                        "role": "assistant",
                        "content": response_text,
                    }
                )

    # Clear conversation button
    if st.session_state.chat_messages:
        if st.button("Clear Conversation", type="secondary"):
            st.session_state.chat_messages = []
            st.session_state.session_id = None
            st.rerun()


# ---------------------------------------------------------------------------
# Knowledge Base Tab
# ---------------------------------------------------------------------------

def render_knowledge_tab() -> None:
    st.header("Knowledge Base Management")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader("Add Documents")

        ingest_method = st.radio(
            "Ingest method",
            ["Upload File", "Enter URL", "Paste Text"],
            horizontal=True,
        )

        if ingest_method == "Upload File":
            uploaded_files = st.file_uploader(
                "Upload documents",
                type=["pdf", "docx", "doc", "csv", "xlsx", "xls", "txt", "md", "zip"],
                help="Supported: PDF, Word (.docx), CSV, Excel (.xlsx), plain text, or a zip of any of these",
                accept_multiple_files=True,
            )
            if uploaded_files and st.button("Ingest Files", type="primary"):
                for uploaded in uploaded_files:
                    with st.spinner(f"Ingesting {uploaded.name}..."):
                        result = api_post(
                            "/knowledge/ingest/file",
                            files={"file": (uploaded.name, uploaded.getvalue(), uploaded.type)},
                        )
                    if result:
                        st.success(f"{result['message']}")

        elif ingest_method == "Enter URL":
            url = st.text_input("URL", placeholder="https://docs.oracle.com/en/cloud/saas/netsuite/...")
            if url and st.button("Ingest URL", type="primary"):
                with st.spinner("Fetching and ingesting..."):
                    result = api_post("/knowledge/ingest/url", json_data={"url": url})
                if result:
                    st.success(result["message"])

        else:  # Paste Text
            source_name = st.text_input("Source name", placeholder="e.g., Customer Import Guide v2")
            text_content = st.text_area(
                "Paste documentation text",
                height=200,
                placeholder="Paste NetSuite documentation, migration notes, or field definitions here...",
            )
            if source_name and text_content and st.button("Ingest Text", type="primary"):
                with st.spinner("Ingesting..."):
                    result = api_post(
                        "/knowledge/ingest/text",
                        json_data={"text": text_content, "source_name": source_name},
                    )
                if result:
                    st.success(result["message"])

    with col2:
        st.subheader("Search Knowledge Base")
        search_query = st.text_input("Search query", placeholder="e.g., required fields for customer import")
        n_results = st.slider("Results", 1, 10, 5)

        if search_query and st.button("Search", type="primary"):
            with st.spinner("Searching..."):
                results = api_get("/knowledge/search", params={"q": search_query, "n": n_results})

            if results is not None:
                if not results:
                    st.info("No results found.")
                else:
                    for i, r in enumerate(results, 1):
                        score_pct = f"{r['score'] * 100:.0f}%"
                        with st.expander(f"Result {i} — {r['source']} (relevance: {score_pct})"):
                            st.text(r["text"][:800])
                            if r.get("metadata"):
                                st.caption(f"Metadata: {r['metadata']}")

    st.divider()

    # ------------------------------------------------------------------
    # NetSuite catalog (structured data model)
    # ------------------------------------------------------------------
    st.subheader("NetSuite Catalog")
    st.caption("Structured record/field knowledge from NetSuite's own metadata. Fed by the Records Browser crawler and by REST metadata exports.")
    cat_col1, cat_col2 = st.columns([1, 1])
    with cat_col1:
        cstats = api_get("/knowledge/catalog/stats")
        if cstats:
            m1, m2, m3 = st.columns(3)
            m1.metric("Record types", cstats.get("record_types", 0))
            m2.metric("Fields", cstats.get("fields", 0))
            m3.metric("Sublists", cstats.get("sublists", 0))
            if cstats.get("by_category"):
                st.caption("By category: " + ", ".join(f"{k} {v}" for k, v in cstats["by_category"].items()))
        lookup = st.text_input("Look up a record type", placeholder="e.g. salesorder, customer, vendorbill")
        if lookup:
            rec = api_get(f"/knowledge/catalog/record/{lookup.strip()}")
            if rec:
                st.write(f"**{rec['id']}** — {rec.get('label') or ''} · {len(rec.get('fields', []))} fields · {len(rec.get('sublists', []))} sublists")
                st.dataframe(
                    [{"Field": f["field_id"], "Label": f.get("label"), "Type": f.get("type"),
                      "Required": f.get("required"), "References": f.get("select_record")}
                     for f in rec.get("fields", [])],
                    use_container_width=True, hide_index=True, height=300,
                )
    with cat_col2:
        st.write("**Import REST metadata export**")
        st.caption("Run `tools/netsuite_extract.py` locally, then upload the zip it produces.")
        zip_file = st.file_uploader("Metadata zip", type=["zip"], key="catalog_zip")
        version = st.text_input("NetSuite release (optional)", placeholder="e.g. 2026.1", key="catalog_version")
        if zip_file and st.button("Import into catalog", type="primary"):
            with st.spinner("Importing schemas..."):
                path = "/knowledge/catalog/import" + (f"?version={version}" if version else "")
                report = api_post(path, files={"file": (zip_file.name, zip_file.getvalue(), "application/zip")})
            if report:
                st.success(
                    f"Imported {report.get('records', 0)} record types, {report.get('fields', 0)} fields, "
                    f"{report.get('sublists', 0)} sublists ({report.get('sublist_fields', 0)} sublist fields)."
                )
                if report.get("errors"):
                    st.warning(f"{len(report['errors'])} errors — first: {report['errors'][0]}")

    st.divider()

    # ------------------------------------------------------------------
    # Crawlers
    # ------------------------------------------------------------------
    st.subheader("Documentation Crawlers")
    cr_col1, cr_col2 = st.columns([1, 2])
    with cr_col1:
        target = st.selectbox("Target", ["all", "docs", "catalog"], help="docs = Oracle Help Center; catalog = NetSuite SOAP schema data model")
        max_pages = st.number_input("Max pages (docs)", min_value=0, value=0, help="0 = use server default")
        if st.button("Start crawl", type="primary"):
            result = api_post("/knowledge/crawl/start", json_data={"target": target, "max_pages": max_pages or None})
            if result:
                st.success("Crawl started." if result.get("started") else f"Not started: {result.get('reason')}")
        if st.button("Refresh status"):
            st.rerun()
    with cr_col2:
        status = api_get("/knowledge/crawl/status")
        if status:
            st.write(f"**Running:** {status.get('running')}")
            for name, job in (status.get("jobs") or {}).items():
                totals = job.get("totals", {}).get("by_status", {})
                line = f"`{name}` — {job.get('state')} · done {totals.get('done', 0)} · failed {totals.get('failed', 0)} · skipped {totals.get('skipped', 0)}"
                if job.get("progress"):
                    p = job["progress"]
                    line += f" · this run: {p.get('pages_crawled', p.get('pages', ''))} pages"
                    if p.get("last_url"):
                        line += f"\n    last: {p['last_url']}"
                if job.get("error"):
                    line += f"\n    error: {job['error']}"
                st.markdown(line)

    st.divider()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    st.subheader("Knowledge Base Statistics")
    if st.button("Refresh Stats"):
        st.rerun()

    stats = api_get("/knowledge/stats")
    if stats:
        st.metric("Total Document Chunks", stats.get("total_documents", 0))
        s1, s2 = st.columns(2)
        with s1:
            if stats.get("doc_types"):
                st.write("**By type:**")
                st.dataframe([{"Type": k, "Chunks": v} for k, v in sorted(stats["doc_types"].items(), key=lambda x: -x[1])],
                             use_container_width=True, hide_index=True)
        with s2:
            if stats.get("modules"):
                st.write("**By module:**")
                st.dataframe([{"Module": k, "Chunks": v} for k, v in sorted(stats["modules"].items(), key=lambda x: -x[1])],
                             use_container_width=True, hide_index=True)
        if stats.get("sources"):
            with st.expander(f"By source ({len(stats['sources'])})"):
                st.dataframe([{"Source": k, "Chunks": v} for k, v in sorted(stats["sources"].items(), key=lambda x: -x[1])],
                             use_container_width=True, hide_index=True)
        else:
            st.info("No documents ingested yet. Upload files or start a crawl above.")


# ---------------------------------------------------------------------------
# Projects Tab
# ---------------------------------------------------------------------------

def render_projects_tab() -> None:
    st.header("Migration Projects")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader("Create New Project")
        with st.form("create_project_form"):
            name = st.text_input("Project Name *", placeholder="e.g., ACME Corp - QuickBooks to NetSuite")
            description = st.text_area("Description", placeholder="Brief description of this migration project")
            source_system = st.selectbox(
                "Source System",
                ["", "quickbooks", "salesforce", "sap", "dynamics", "oracle", "other"],
            )
            submitted = st.form_submit_button("Create Project", type="primary")

            if submitted and name:
                result = api_post(
                    "/projects",
                    json_data={
                        "name": name,
                        "description": description or None,
                        "source_system": source_system or None,
                    },
                )
                if result:
                    st.success(f"Project created: {result['name']} (ID: {result['id'][:16]}...)")
                    st.session_state.projects_cache = []  # Invalidate cache
                    st.rerun()

    with col2:
        st.subheader("Existing Projects")
        projects = api_get("/projects")
        if projects:
            for p in projects:
                with st.expander(f"{p['name']} — {p.get('source_system', 'N/A')}"):
                    st.caption(f"ID: {p['id']}")
                    if p.get("description"):
                        st.write(p["description"])
                    st.caption(f"Created: {p.get('created_at', '')[:10]}")

                    if st.button(f"View Details", key=f"view_{p['id']}"):
                        detail = api_get(f"/projects/{p['id']}")
                        if detail:
                            st.write(f"**Notes ({len(detail.get('notes', []))}):**")
                            for note in detail.get("notes", [])[:10]:
                                st.markdown(f"- [{note['note_type']}] {note['content'][:200]}")

                            mappings = detail.get("field_mappings", [])
                            if mappings:
                                st.write(f"**Field Mappings ({len(mappings)}):**")
                                mapping_data = [
                                    {
                                        "Source": m["source_field"],
                                        "NetSuite Field": m["netsuite_field"],
                                        "Record Type": m["netsuite_record_type"],
                                    }
                                    for m in mappings[:20]
                                ]
                                st.dataframe(mapping_data, use_container_width=True, hide_index=True)
        else:
            st.info("No projects yet.")


# ---------------------------------------------------------------------------
# Data Tools Tab
# ---------------------------------------------------------------------------

def render_data_tools_tab() -> None:
    st.header("Data Tools")

    tool_option = st.radio(
        "Tool",
        ["Field Mapper", "CSV Validator", "CSV Transformer"],
        horizontal=True,
    )

    if tool_option == "Field Mapper":
        _render_field_mapper()
    elif tool_option == "CSV Validator":
        _render_validator()
    else:
        _render_transformer()


def _render_field_mapper() -> None:
    st.subheader("Field Mapper")
    st.caption("Map source system fields to NetSuite field IDs")

    col1, col2 = st.columns(2)
    with col1:
        source_system = st.selectbox(
            "Source System",
            ["generic", "quickbooks", "salesforce", "sap", "dynamics"],
        )
    with col2:
        record_type = st.selectbox(
            "Target Record Type",
            ["customer", "vendor", "inventoryitem", "invoice", "employee", "journalentry", "salesorder", "contact"],
        )

    fields_input = st.text_area(
        "Source field names (one per line)",
        height=150,
        placeholder="AccountName\nEmailAddress\nPhoneNumber\nBillingStreet\n...",
    )

    if fields_input and st.button("Suggest Mappings", type="primary"):
        source_fields = [f.strip() for f in fields_input.split("\n") if f.strip()]

        with st.spinner("Mapping fields..."):
            result = api_post(
                "/chat",
                json_data={
                    "message": f"Please map these {source_system} source fields to NetSuite {record_type} fields:\n{', '.join(source_fields)}\n\nUse the map_fields tool.",
                    "session_id": st.session_state.session_id,
                    "project_id": st.session_state.project_id,
                },
            )

        if result:
            st.session_state.session_id = result.get("session_id")
            st.markdown(result.get("response", ""))


def _render_validator() -> None:
    st.subheader("CSV Validator")
    st.caption("Validate a migration CSV against NetSuite field rules")

    record_type = st.selectbox(
        "Record Type",
        ["customer", "vendor", "inventoryitem", "invoice", "employee", "journalentry", "salesorder", "contact"],
        key="validator_record_type",
    )

    uploaded = st.file_uploader(
        "Upload CSV to validate",
        type=["csv"],
        key="validator_upload",
    )

    if uploaded and st.button("Validate CSV", type="primary"):
        import tempfile

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(uploaded.getvalue())
            tmp_path = tmp.name

        with st.spinner("Validating..."):
            result = api_post(
                "/chat",
                json_data={
                    "message": f"Please validate the CSV file at '{tmp_path}' as a {record_type} import file. Use the validate_csv_file tool.",
                    "session_id": st.session_state.session_id,
                    "project_id": st.session_state.project_id,
                },
            )

        import os
        os.unlink(tmp_path)

        if result:
            st.session_state.session_id = result.get("session_id")
            response = result.get("response", "")
            if "error" in response.lower() or "Error" in response:
                st.error(response)
            else:
                st.markdown(response)


def _render_transformer() -> None:
    st.subheader("CSV Transformer")
    st.caption("Transform a source CSV into a NetSuite Import-ready file")

    col1, col2 = st.columns(2)
    with col1:
        source_system = st.selectbox(
            "Source System",
            ["generic", "quickbooks", "salesforce", "sap", "dynamics"],
            key="transformer_source",
        )
    with col2:
        record_type = st.selectbox(
            "Target Record Type",
            ["customer", "vendor", "inventoryitem", "invoice", "employee", "journalentry", "salesorder", "contact"],
            key="transformer_record_type",
        )

    uploaded = st.file_uploader(
        "Upload source CSV",
        type=["csv"],
        key="transformer_upload",
    )

    if uploaded:
        import tempfile
        import pandas as pd

        # Preview
        try:
            df_preview = pd.read_csv(io.BytesIO(uploaded.getvalue()), nrows=5)
            st.write("**Preview (first 5 rows):**")
            st.dataframe(df_preview, use_container_width=True)
            st.caption(f"Columns: {', '.join(df_preview.columns)}")
        except Exception as exc:
            st.warning(f"Could not preview CSV: {exc}")

        if st.button("Transform & Generate Import File", type="primary"):
            from data_tools.transformer import DataTransformer

            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                tmp.write(uploaded.getvalue())
                tmp_path = tmp.name

            out_path = tmp_path.replace(".csv", "_netsuite.csv")

            with st.spinner("Transforming..."):
                try:
                    transformer = DataTransformer()
                    transformed_df, out = transformer.auto_transform_csv(
                        tmp_path, record_type, out_path, source_system
                    )

                    st.success(f"Transformation complete! {len(transformed_df)} rows.")
                    st.write("**Transformed data preview:**")
                    st.dataframe(transformed_df.head(10), use_container_width=True)

                    # Download button
                    csv_bytes = transformed_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                    st.download_button(
                        label="Download NetSuite Import CSV",
                        data=csv_bytes,
                        file_name=f"{Path(uploaded.name).stem}_netsuite_import.csv",
                        mime="text/csv",
                        type="primary",
                    )
                except Exception as exc:
                    st.error(f"Transform error: {exc}")
                finally:
                    import os
                    os.unlink(tmp_path)
                    if Path(out_path).exists():
                        os.unlink(out_path)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def main() -> None:
    render_sidebar()

    tab1, tab2, tab3, tab4 = st.tabs(["Chat", "Knowledge Base", "Projects", "Data Tools"])

    with tab1:
        render_chat_tab()

    with tab2:
        render_knowledge_tab()

    with tab3:
        render_projects_tab()

    with tab4:
        render_data_tools_tab()


if __name__ == "__main__":
    main()
