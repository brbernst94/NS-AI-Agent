# NS-AI-Agent: NetSuite Systems Expert

An AI agent that knows out-of-the-box NetSuite end to end and answers as a systems expert: the data model (every record, field, sublist), setup, accounting/OneWorld, order-to-cash, inventory, CSV imports, SuiteScript, SuiteFlow, SuiteTalk/REST, and the practical gotchas. Product direction: license it to businesses, who then connect their own NetSuite (read-only) so the agent learns their customisations. Multi-tenant fields (`tenant_id`) are already in the schema; the customer layer is not built yet.

## Architecture

Everything persistent lives in PostgreSQL (Railway managed Postgres, `DATABASE_URL`). No local files, no GitHub data sync.

### Knowledge (`knowledge_base/`)
- `db.py` — shared `ThreadedConnectionPool`, pgvector registration, and **all DDL** (`init_schema()`). Idempotent; runs at startup.
- `index.py` — `KnowledgeIndex`: pgvector semantic search over `knowledge_documents`. Chunks carry tag columns `system, module, doc_type, version, title, url, tenant_id`. `search(query, n, where={...}, tenant_id=...)` filters on tags; shared rows (`tenant_id IS NULL`) are always visible and a tenant's own rows get a small rank boost. Embeddings: `all-MiniLM-L6-v2` (384-d), HNSW cosine index (IVFFlat/sequential fallback).
- `catalog.py` — `NetSuiteCatalog`: the **structured data model** in `ns_record_types`, `ns_fields`, `ns_sublists`, `ns_sublist_fields`. Upserts from any source; `resolve_record_type`, `get_record`, `get_field`, `find_fields(keyword)`, `stats`, and text formatters for the agent. `tenant_id=''` means standard NetSuite.
- `metadata_import.py` — parses NetSuite REST metadata-catalog JSON Schemas (zip from `tools/netsuite_extract.py`) into the catalog. Handles required/enum/maxLength/readOnly/custom flags and sublist collection→element references.
- `records_browser.py` — crawls the public NetSuite Records Browser (`system.netsuite.com/.../srbrowser/`) into the catalog. Header-driven table parsing; tries release versions newest-first.
- `crawler.py` — Oracle Help Center crawler. Tags each page with a `module` inferred from title/URL (`suitescript`, `csv_import`, `accounting`, ...). Resumable: state in `crawl_urls`. Page cap `CRAWL_MAX_PAGES` (default 5000), delay `CRAWL_DELAY_SECONDS`.
- `crawl_state.py` — `crawl_urls` bookkeeping. `crawl_manager.py` — background runner with live status.
- `ingest.py` — PDF/Word/CSV/URL/text ingestion; every path accepts `tags`.

### Agent (`agent/`)
- `core.py` — Anthropic tool-use loop; owns `AgentMemory`, `KnowledgeIndex`, `NetSuiteCatalog`. Extracts learnings from chats into the KB (`doc_type=chat_learning`).
- `tools.py` — tools: `search_knowledge_base` (module/doc_type filters), `get_netsuite_field_info` (catalog first, hand-written registry notes appended), `find_netsuite_field`, `list_netsuite_record_types`, CSV validate/map/transform/generate, project context/notes.
- `memory.py` — PostgreSQL conversation + project memory (shares the pool from `knowledge_base/db.py`).
- `prompts.py` — systems-expert persona; terse conversational style; tool order: catalog → docs → project context.
- `github_sync.py` — no-op stubs kept for old imports.

### Other
- `netsuite/field_registry.py` — hand-curated migration notes for 8 record types. Now a fallback/enrichment behind the catalog, still used by `data_tools/`.
- `data_tools/` — CSV validator, field mapper, transformer.
- `api/` — FastAPI. `main.py` starts in degraded mode (503s + `/health.startup_error`) if init fails instead of crashing. `CRAWL_ON_STARTUP=0` disables the automatic crawl.
- `web/app.py` — Streamlit UI (Chat, Knowledge Base incl. catalog + crawler controls, Projects, Data Tools). Talks to the API at `NS_AGENT_API`.
- `cli/main.py` — Click CLI.
- `eval/` — `netsuite_qa.jsonl` question set and `run_eval.py` (keyword + Claude-judge scoring against `/chat`).
- `tools/netsuite_extract.py` — run locally with TBA credentials to export the REST metadata catalog as a zip.

## API

- `GET /health` — status, KB chunk count, catalog stats, startup error.
- `POST /chat` · `GET /chat/history/...`
- `POST /knowledge/ingest/file|url|text` (optional `module`) · `GET /knowledge/search?q=&n=&module=&doc_type=` · `GET /knowledge/stats` · `DELETE /knowledge/source/{name}`
- `GET /knowledge/catalog/stats` · `GET /knowledge/catalog/records?category=` · `GET /knowledge/catalog/record/{id}` · `GET /knowledge/catalog/fields?q=&record_type=` · `POST /knowledge/catalog/import` (zip)
- `POST /knowledge/crawl/start {target: docs|records_browser|all, max_pages}` · `GET /knowledge/crawl/status`
- `/projects/*` CRUD + notes

## Deployment (Railway)

Three services from one repo:
- **Postgres** — managed; pgvector extension is created by the app.
- **web** — `SERVICE_ROLE=api`, `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `ANTHROPIC_API_KEY`. `start.sh` runs uvicorn on `$PORT`.
- **streamlit** — `SERVICE_ROLE=streamlit`, `NS_AGENT_API=https://<web service url>`.

`railway.json` runs `sh start.sh` for both; `SERVICE_ROLE` picks what starts. Unset (local dev) runs both.

## Feeding the NetSuite knowledge

1. **Records Browser + Help Center** crawl automatically on startup (or via the Knowledge Base tab / `POST /knowledge/crawl/start`). Progress in the UI or `GET /knowledge/crawl/status`.
2. **REST metadata catalog** (most authoritative): `pip install requests requests-oauthlib`, set `NS_ACCOUNT`, `NS_CONSUMER_KEY`, `NS_CONSUMER_SECRET`, `NS_TOKEN_ID`, `NS_TOKEN_SECRET`, run `python tools/netsuite_extract.py`, upload the zip in the UI. Custom fields in the export are flagged `is_custom`.
3. **Manual uploads** — Help Center PDFs, SuiteAnswers articles, notes. Tag with a `module` where possible.
4. **Measure**: `python eval/run_eval.py --api <web url>`; add questions to `eval/netsuite_qa.jsonl` as gaps are found.

## Local development

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/nsagent   # needs pgvector
export ANTHROPIC_API_KEY=sk-...
sh start.sh          # API on 8000 + Streamlit on 8501
```

## Conventions

- All DDL goes in `knowledge_base/db.py` (`_KNOWLEDGE_DDL` / `_CATALOG_DDL`) as idempotent statements; memory tables in `agent/memory.py`.
- Never re-add file-based persistence; the container filesystem is ephemeral.
- New knowledge sources must set `doc_type` and, where possible, `module`; catalog writers must set `source`.
- The agent's style rules live in `agent/prompts.py` — short, conversational, no headers/bullets unless asked.
