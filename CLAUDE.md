# NS-AI-Agent: NetSuite Systems Expert

An AI agent that knows out-of-the-box NetSuite end to end and answers as a systems expert: the data model (every record, field, sublist), setup, accounting/OneWorld, order-to-cash, inventory, CSV imports, SuiteScript, SuiteFlow, SuiteTalk/REST, and the practical gotchas. Product direction: license it to businesses, who then connect their own NetSuite (read-only) so the agent learns their customisations. Multi-tenant fields (`tenant_id`) are already in the schema; the customer layer is not built yet.

## Architecture

Everything persistent lives in PostgreSQL (Railway managed Postgres, `DATABASE_URL`). No local files, no GitHub data sync.

### Knowledge (`knowledge_base/`)
- `db.py` — shared `ThreadedConnectionPool`, pgvector registration, and **all DDL** (`init_schema()`). Idempotent; runs at startup.
- `index.py` — `KnowledgeIndex`: pgvector semantic search over `knowledge_documents`. Chunks carry tag columns `system, module, doc_type, version, title, url, tenant_id`. `search(query, n, where={...}, tenant_id=...)` filters on tags; shared rows (`tenant_id IS NULL`) are always visible and a tenant's own rows get a small rank boost. Embeddings: `all-MiniLM-L6-v2` (384-d), HNSW cosine index (IVFFlat/sequential fallback).
- `catalog.py` — `NetSuiteCatalog`: the **structured data model** in `ns_record_types`, `ns_fields`, `ns_sublists`, `ns_sublist_fields`. Upserts from any source; `resolve_record_type`, `get_record`, `get_field`, `find_fields(keyword)`, `stats`, and text formatters for the agent. `tenant_id=''` means standard NetSuite.
- `metadata_import.py` — parses NetSuite REST metadata-catalog JSON Schemas (zip from `tools/netsuite_extract.py`) into the catalog. Handles required/enum/maxLength/readOnly/custom flags and sublist collection→element references.
- `soap_schema.py` — builds the catalog from NetSuite's **public SuiteTalk SOAP schemas** (`webservices.netsuite.com/wsdl/<version>/netsuite.wsdl` plus its 38 XSDs, no authentication). Record types are `complexType`s extending `platformCore:Record`; fields are their elements; sublists are `...List` wrappers around a repeated item type; enums come from `simpleType` restrictions. Yields ~183 record types and ~6,100 fields in seconds. Replaced the Records Browser crawler, which NetSuite took offline (every version now redirects to page_not_found).
- `crawler.py` — Oracle Help Center crawler over `docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/` (**not** `ns_en`, which 404s). Seeded from the 16 live section landing pages. Tags each page with a `module` inferred from title/URL. Resumable: state in `crawl_urls`. Page cap `CRAWL_MAX_PAGES` (default 5000), delay `CRAWL_DELAY_SECONDS`.
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
- `eval/` — `netsuite_qa.jsonl` (lookup questions), `netsuite_hard.jsonl` (judgment questions, including five written by the product owner and tagged `source: founder`) and `run_eval.py` (keyword + Claude-judge scoring against `/chat`). The judge needs `ANTHROPIC_API_KEY` in the environment running the eval; without it every `judge_pass` is null and only the near-meaningless keyword score is produced.
- `docs/BRAIN.md` — the state of turning the crawled corpus into expertise, and what to do next. Read it before working on distillation or the eval.
- `tools/netsuite_extract.py` — run locally with TBA credentials to export the REST metadata catalog as a zip.

## API

- `GET /health` — status, KB chunk count, catalog stats, startup error.
- `POST /chat` · `GET /chat/history/...`
- `POST /knowledge/ingest/file|url|text` (optional `module`) · `GET /knowledge/search?q=&n=&module=&doc_type=` · `GET /knowledge/stats` · `DELETE /knowledge/source/{name}`
- `GET /knowledge/catalog/stats` · `GET /knowledge/catalog/records?category=` · `GET /knowledge/catalog/record/{id}` · `GET /knowledge/catalog/fields?q=&record_type=` · `POST /knowledge/catalog/import` (zip)
- `POST /knowledge/crawl/start {target: docs|catalog|all, max_pages}` · `GET /knowledge/crawl/status`
- `GET /knowledge/crawl/health?hours=6` — **is the knowledge base still growing?** Compares current counts against a `crawl_snapshots` row from N hours ago, folds in `crawl_runs` history and the actual error strings, returns a verdict (`healthy`, `running`, `complete`, `needs_attention`, `failing`, `empty`, `no_history`) plus a paste-ready `summary`. Works in degraded mode — it does not require the agent.
- `/projects/*` CRUD + notes

## Deployment (Railway)

Three services from one repo:
- **Postgres** — managed; pgvector extension is created by the app.
- **web** — `SERVICE_ROLE=api`, `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `ANTHROPIC_API_KEY`. `start.sh` runs uvicorn on `$PORT`.
- **streamlit** — `SERVICE_ROLE=streamlit`, `NS_AGENT_API=https://<web service url>`.

`railway.json` runs `sh start.sh` for both; `SERVICE_ROLE` picks what starts. Unset (local dev) runs both.

## Operations

Live services:
- API (`web`): `https://web-production-5ff2c9.up.railway.app`
- UI (`streamlit`): `https://streamlit-production-d431.up.railway.app`
- Work happens on branch `claude/funny-ride-Oq16Q`.

**Is the brain still growing?** `GET /knowledge/crawl/health?hours=6`. Read `verdict` and `summary`; `per_crawler`, `detail.top_errors` and `last_runs` carry the evidence. What each verdict means and the fix:

| verdict | meaning | action |
|---|---|---|
| `healthy` / `running` | counts grew in the window, or a crawl is in flight | nothing |
| `complete` | both crawlers exhausted their queues | nothing; add sources to grow further |
| `empty` | no run has ever started | check `CRAWL_ON_STARTUP`, then `POST /knowledge/crawl/start` |
| `failing` (catalog) | the SOAP import ran but produced no record types | NetSuite's schema layout changed; re-check `soap_schema.py` against a live XSD |
| `run_error` (catalog) | the schemas moved or are unreachable | check `webservices.netsuite.com/wsdl/<version>/netsuite.wsdl`; bump `NS_SOAP_VERSION` |
| `failing` with HTTP 403/429 (docs) | Oracle is blocking the crawler | adjust User-Agent, raise `CRAWL_DELAY_SECONDS`, or move to a sitemap-driven crawl |
| `stalled` with "too short" skips | `_extract_page` isn't finding the content element | fix the selector against real HTML |
| `run_error` | the run raised | read `last_runs[*].error` |

**Database unreachable.** If `/health` shows `could not translate host name "postgres.railway.internal"`, the web service cannot use Railway's private network — nothing is stored and no crawl runs, because agent init fails. Fix: set the web service's `DATABASE_URL` to `${{Postgres.DATABASE_PUBLIC_URL}}` (public proxy, always resolves) instead of `${{Postgres.DATABASE_URL}}`. The app no longer stays degraded forever after such a failure: `_initialize` in `api/main.py` retries in the background with backoff (20s doubling to 5 min) and recovers on its own once the database answers; `/health` reports `status: starting` and `init_attempts` while it does.

Known constraint: **the account owner does not run scripts against NetSuite.** `tools/netsuite_extract.py` exists but is not used. Feed the catalog from the public crawlers, and knowledge from zip uploads (a zip of PDFs/Word/CSV/text is unpacked and every supported file ingested).

Sandbox note: agent containers capture their network policy at start. If Railway is unreachable ("no rule allows host"), the policy changed after this container booted — a newly started session picks it up.

## Feeding the NetSuite knowledge

1. **SOAP schema catalog + Help Center** run automatically on startup (or via the Knowledge Base tab / `POST /knowledge/crawl/start`). Progress in the UI or `GET /knowledge/crawl/status`. The catalog import takes seconds; the docs crawl runs for hours and resumes where it left off.
2. **REST metadata catalog** (optional, adds an account's custom fields): the owner does not run scripts against NetSuite, so `tools/netsuite_extract.py` is unused. It remains the path for a future customer connector — its output uploads via `POST /knowledge/catalog/import` and flags custom fields `is_custom`.
3. **Manual uploads** — Help Center PDFs, SuiteAnswers articles, notes. Tag with a `module` where possible.
4. **Measure**: `python eval/run_eval.py --api <web url> --file eval/netsuite_hard.jsonl`; confirm the run prints a `Judge score:` line, or the result is keyword-only and does not measure expertise. Add questions as gaps are found — judgment questions to `netsuite_hard.jsonl`, lookups to `netsuite_qa.jsonl`.

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
