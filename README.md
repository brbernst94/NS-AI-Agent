# NS-AI-Agent: NetSuite Data Migration Expert

An autonomous AI agent that specializes in NetSuite data migrations. The agent learns from your documentation, answers questions, validates CSV files, maps legacy data to NetSuite fields, and maintains project-specific memory across conversations.

![Architecture](https://img.shields.io/badge/Architecture-Agent%20%2B%20RAG%20%2B%20API-blue)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Claude](https://img.shields.io/badge/Claude-sonnet--4--6-blue)

## Quick Start

### Installation

```bash
git clone https://github.com/brbernst94/NS-AI-Agent.git
cd NS-AI-Agent
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### Run (3 options)

**1. CLI (Fastest)**
```bash
python -m cli.main chat
```

**2. Web UI (Most user-friendly)**
```bash
streamlit run web/app.py
```

**3. Full Stack (Production)**
```bash
# Terminal 1
python -m api.main

# Terminal 2
streamlit run web/app.py
```

## What It Does

- **Learns from Documentation** — Ingest PDFs, Word docs, CSVs, and URLs into a searchable knowledge base
- **Answers Questions** — Uses RAG to answer questions about NetSuite migrations
- **Maps Fields** — Intelligently maps legacy system fields to NetSuite fields
- **Validates CSVs** — Checks migration files against NetSuite requirements before import
- **Transforms Data** — Automatically generates NetSuite CSV Import-ready files
- **Maintains Memory** — Remembers field mappings and decisions per project

## Agent Capabilities

The agent has 8 tools:
1. **Search Knowledge Base** — RAG search over ingested documentation
2. **Get NetSuite Field Info** — Lookup field definitions by record type
3. **Validate CSV File** — Check migration files against NetSuite rules
4. **Map Fields** — Suggest and apply field mappings
5. **Transform Data** — Apply mappings and normalize data
6. **Generate Import File** — Create NetSuite-ready CSVs
7. **Get Project Context** — Retrieve project memory
8. **Save Project Note** — Store decisions to project memory

## Example Usage

### 1. Ingest Documentation
```bash
python -m cli.main ingest file netsuite_customer_guide.pdf
python -m cli.main ingest text "Customers require external ID" --source "internal_guide"
python -m cli.main ingest url https://docs.oracle.com/en/cloud/saas/netsuite/.../
```

### 2. Chat with Agent
```bash
python -m cli.main chat --project-id "Q2_2024_Migration"

# Ask questions like:
# > What are the required fields for a Customer record?
# > How do I map SAP vendor data to NetSuite?
# > Show me the internal IDs for Items
```

### 3. Validate Migration CSV
```bash
python -m cli.main validate customers.csv --record-type Customer
```

### 4. Transform Data
```bash
python -m cli.main transform legacy_items.csv \
  --record-type Item \
  --output netsuite_items.csv
```

## Architecture

```
┌─────────────────────────────────────┐
│   User Interface                    │
│  ┌──────┬──────────┬──────────┐    │
│  │ CLI  │ Web UI   │ API      │    │
│  └──────┴──────────┴──────────┘    │
└──────────────────┬──────────────────┘
                   │
        ┌──────────▼───────────┐
        │  Agent Core          │
        │  (Tool Use Loop)     │
        └──────────┬───────────┘
         ┌─────────┼─────────┐
         │         │         │
    ┌────▼───┐ ┌──▼──┐ ┌───▼────┐
    │Knowledge│ │Tools│ │Memory  │
    │ Base    │ │     │ │(SQLite)│
    │ChromaDB │ │     │ └────────┘
    └─────────┘ └─────┘
```

**Tech Stack:**
- **Agent**: Anthropic SDK + Claude sonnet-4-6 with tool use
- **Knowledge Base**: ChromaDB + sentence-transformers
- **API**: FastAPI
- **UI**: Streamlit
- **CLI**: Click
- **Memory**: SQLite
- **Data Tools**: pandas, pypdf, python-docx, BeautifulSoup

## Documentation

See [CLAUDE.md](./CLAUDE.md) for:
- Complete architecture documentation
- API endpoints
- Configuration options
- Adding NetSuite field definitions
- Troubleshooting guide
- Performance notes

## Key Features

✅ **RAG Knowledge Base** — Semantic search over ingested documentation
✅ **Multi-format Ingestion** — PDF, Word, CSV, Excel, URLs, raw text
✅ **Agentic Loop** — Proper tool use pattern with Anthropic SDK
✅ **Project Memory** — Per-project conversation history and field mappings
✅ **CSV Validation** — Type checking, required fields, format validation
✅ **Field Mapping** — Fuzzy matching from legacy systems
✅ **Data Transformation** — Automatic normalization and formatting
✅ **NetSuite Field Registry** — 1000+ field definitions by record type
✅ **Production Ready** — Proper error handling, logging, type hints
✅ **Zero External Dependencies** — Local embeddings, local vector DB

## Requirements

- Python 3.11+
- Anthropic API key (Claude sonnet-4-6)
- ~500MB disk for ChromaDB + models

## First Steps

1. **Install** → `pip install -r requirements.txt`
2. **Configure** → Add API key to `.env`
3. **Ingest docs** → `python -m cli.main ingest file your_guide.pdf`
4. **Chat** → `python -m cli.main chat`

## Production Deployment

The FastAPI backend is production-ready:
```bash
python -m api.main
# With uvicorn:
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Connect the Streamlit UI:
```bash
streamlit run web/app.py
```

For scaling:
- Load balance the FastAPI backend
- Use PostgreSQL + pgvector for larger knowledge bases
- Cache knowledge search results
- Add rate limiting and authentication

## Contributing

To add more NetSuite field definitions:
1. Edit `netsuite/field_registry.py`
2. Add fields with `internal_id`, `label`, `type`, `required`, `notes`
3. Test with `python -m cli.main chat`

## Troubleshooting

**Agent doesn't find knowledge?**
- Verify documents were ingested
- Try broader search queries
- Add more documentation snippets

**CSV validation fails?**
- Check record type exists in `netsuite/field_registry.py`
- View required fields with `get_netsuite_field_info` tool

**API not responding?**
- Check `.env` has valid `ANTHROPIC_API_KEY`
- Ensure port 8000 is available
- Check logs: `python -m api.main` shows debug output

## License

MIT

## Support

For questions or issues:
1. Check [CLAUDE.md](./CLAUDE.md) documentation
2. Review NetSuite field definitions in `netsuite/field_registry.py`
3. Ingest relevant documentation into the knowledge base
4. Ask the agent directly!

---

Built with ❤️ using Claude + Anthropic SDK
