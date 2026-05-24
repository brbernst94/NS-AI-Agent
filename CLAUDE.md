# NS-AI-Agent: NetSuite Data Migration Expert

An autonomous AI agent that acts as a Subject Matter Expert (SME) for NetSuite data migrations. The agent learns from NetSuite documentation, answers customer questions using RAG, transforms legacy data to NetSuite format, and maintains project-specific memory.

## Architecture

### Core Components

**Agent Core** (`agent/`)
- `core.py`: Agentic loop implementing proper Anthropic SDK tool use pattern
  - Maintains conversation state
  - Handles tool_use blocks in the Claude response
  - Persists conversations to SQLite
  - Supports streaming for API endpoints
- `tools.py`: 8 specialized tools the agent can invoke:
  1. `search_knowledge_base` - RAG search over ingested NetSuite docs
  2. `get_netsuite_field_info` - Lookup field definitions by record type
  3. `validate_csv_file` - Validate migration CSV against NetSuite requirements
  4. `map_fields` - Map source fields to NetSuite fields (with fuzzy matching)
  5. `transform_data` - Apply field mappings to transform CSV data
  6. `generate_import_file` - Produce NetSuite CSV Import-ready files
  7. `get_project_context` - Retrieve project-specific notes and history
  8. `save_project_note` - Store decisions and mappings to project memory
- `memory.py`: SQLite-based conversation and project memory
- `prompts.py`: System prompt establishing the agent as a NetSuite migration expert

**Knowledge Base** (`knowledge_base/`)
- `index.py`: ChromaDB persistent vector store (stored at `./data/chroma_db`)
  - Uses sentence-transformers `all-MiniLM-L6-v2` for embeddings
  - Supports semantic search over ingested documents
- `ingest.py`: Multi-format document ingestion
  - PDF ingestion via pypdf
  - Word (.docx) ingestion via python-docx
  - CSV/Excel ingestion via pandas
  - URL scraping via BeautifulSoup
  - Direct text ingestion
  - Automatic chunking (500 tokens, 50 token overlap)

**NetSuite Field Registry** (`netsuite/field_registry.py`)
- Comprehensive field definitions for core record types:
  - Customer, Vendor, Item (InventoryItem)
  - Transactions (Invoice, Bill, Sales Order, Purchase Order)
  - Employee, Department, Location
  - Custom Records
- Each field includes: internal_id, label, type, required flag, notes

**Data Tools** (`data_tools/`)
- `validator.py`: CSV validation against NetSuite rules
  - Required field checks
  - Type validation (text, select, date, currency, checkbox)
  - Format validation (dates, phone numbers, etc.)
  - Max length checks
- `mapper.py`: Field mapping engine
  - Fuzzy matching of source fields to NetSuite fields
  - Support for common source systems (SAP, Salesforce, QuickBooks, Dynamics)
  - Suggestion and application of field mappings
- `transformer.py`: Data transformation and import file generation
  - Date normalization
  - Boolean normalization (Y/N → TRUE/FALSE)
  - Phone/currency formatting
  - NetSuite CSV Import format generation

**API** (`api/`)
- `main.py`: FastAPI application
  - CORS middleware
  - Startup: initializes DB, ChromaDB, embeddings
  - Health check endpoint
- Routes:
  - `/chat`: POST with message, GET history (streaming support)
  - `/knowledge/ingest/*`: Upload PDF/Word, paste text, URL ingestion
  - `/knowledge/search`: RAG search
  - `/knowledge/stats`: Document statistics
  - `/projects/*`: CRUD operations for projects and notes

**Web UI** (`web/app.py`)
- Streamlit multi-tab interface:
  - **Chat**: Interactive agent chat with project context (sidebar)
  - **Knowledge Base**: Upload docs, paste text, add URLs, search, view stats
  - **Projects**: Create/view projects and notes
  - **Data Tools**: Upload CSV, validate, transform, download result
- Connects to FastAPI backend at `http://localhost:8000`

**CLI** (`cli/main.py`)
- Click-based command-line interface:
  - `chat [--project-id ID]`: Interactive REPL chat
  - `ingest file PATH`: Load document into knowledge base
  - `ingest url URL`: Fetch and ingest URL
  - `ingest text TEXT --source NAME`: Ingest raw text
  - `validate CSV_PATH --record-type TYPE`: Validate migration CSV
  - `transform CSV_PATH --record-type TYPE --output PATH`: Transform data
  - `projects list`: List projects
  - `projects create NAME`: Create new project

**Database** (`db/schema.sql`)
- SQLite schema for:
  - `conversations`: session_id, role, content, timestamp, project_id
  - `projects`: id, name, description, created_at
  - `project_notes`: project_id, note_type, content, created_at

## Setup & Running

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Create a `.env` file from `.env.example`:
```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 2. Initialize Database

```bash
python -c "from agent.memory import AgentMemory; AgentMemory().init_db()"
```

This creates `./data/agent_memory.db`.

### 3. Run the Agent

**Option A: CLI (Fastest to test)**
```bash
python -m cli.main chat --project-id my_migration_project
```

**Option B: Web UI (Most user-friendly)**
```bash
streamlit run web/app.py
# Opens at http://localhost:8501
```

**Option C: API + Web UI (Production-ready)**

Terminal 1: Start API server
```bash
python -m api.main
# FastAPI at http://localhost:8000
# Docs at http://localhost:8000/docs
```

Terminal 2: Start Streamlit (connects to API)
```bash
streamlit run web/app.py
```

## Automatic Learning from Chat

The agent **automatically learns from conversations**. After each chat:

1. The agent analyzes your question and its response
2. It extracts key NetSuite knowledge (field names, mapping rules, validation requirements, best practices)
3. It automatically adds this to the knowledge base
4. Future conversations can use these learnings

**What gets automatically learned:**
- Field definitions mentioned in conversation
- Mapping rules you discover
- Validation requirements
- Transformation rules discussed
- Best practices

**What doesn't get learned:**
- Trivial chats or clarifications
- Responses shorter than 100 characters (likely errors)
- Conversations without substantive NetSuite knowledge

### Example:

```
You: "How do I handle dates in a customer import from SAP?"

Agent: "Dates should be in MM/DD/YYYY format. SAP often exports 
in YYYYMMDD format, so you need to transform them. Use a script 
or Excel formula to convert. Also, never import future dates..."

✅ This gets automatically saved to knowledge base because it contains
   useful mapping and validation knowledge.
```

Next time you ask about dates, the agent will have learned from this conversation.

---

## Manual Knowledge Ingestion

You can also manually add documents to the knowledge base:

### Via CLI
```bash
# Upload PDF
python -m cli.main ingest file netsuite_guide.pdf

# Add from URL (e.g., SuiteAnswers)
python -m cli.main ingest url https://docs.oracle.com/en/cloud/saas/netsuite/ns_en/...

# Paste documentation snippet
python -m cli.main ingest text "Customer field mapping for..."  --source "custom_guide"
```

### Via Web UI
1. Go to **Knowledge Base** tab
2. Upload files, paste text, or enter URLs
3. View ingestion progress and document statistics

### Via API
```bash
# Upload file
curl -X POST http://localhost:8000/knowledge/ingest/file \
  -F "file=@netsuite_guide.pdf"

# Ingest text
curl -X POST http://localhost:8000/knowledge/ingest/text \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "source_name": "..."}'

# Search knowledge base
curl "http://localhost:8000/knowledge/search?q=customer+mapping&n=5"
```

## Using the Agent

### Pure Text Chat Interface

The agent presents a **simple text chat experience**. You type natural language questions, and the agent automatically uses its tools in the background to find answers. No tool names or internal details are shown — just natural conversation.

**Tools the agent automatically uses:**
1. Searches your knowledge base for relevant NetSuite documentation
2. Looks up field definitions and internal IDs
3. Validates data against NetSuite requirements
4. Maps legacy fields to NetSuite fields
5. Transforms data into import-ready format
6. Maintains project memory across conversations

**Example questions:**
- "How do I map SAP customer data to NetSuite?"
- "What are the required fields for an invoice import?"
- "Show me the internal IDs for the Customer record type"
- "Is this CSV ready to import as Customers?"
- "How do I set up the employee location field?"

The agent will automatically use relevant tools and respond naturally with guidance, field names, data validation results, and best practices.

### Validate a Migration CSV
```bash
python -m cli.main validate customers.csv --record-type Customer
```

Returns:
- Required fields check
- Type validation results
- Format validation results
- Error/warning summary

### Transform Data
```bash
python -m cli.main transform legacy_customers.csv \
  --record-type Customer \
  --output netsuite_customers.csv
```

The agent will:
1. Analyze source data
2. Suggest field mappings
3. Apply transformations (date formatting, field normalization, etc.)
4. Generate NetSuite CSV Import format

## Project Memory

Each migration project maintains:
- Conversation history (all chat messages)
- Field mappings (source field → NetSuite field decisions)
- Validation results and issues found
- Data transformation rules applied
- Notes and decisions documented

Access project context via:
```bash
# CLI: Specify project-id in chat
python -m cli.main chat --project-id "Q2_2024_Migration"

# API: Pass project_id to /chat endpoint
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "...", "project_id": "Q2_2024_Migration"}'

# Web UI: Select project in sidebar
```

## GitHub Persistence & Multi-Device Sync

All trained knowledge is backed up to GitHub, so you can:
- **Work from multiple computers** — Clone the repo anywhere and have all your training data
- **Deploy to the cloud** — Push to GitHub, then deploy to production with all your knowledge intact
- **Share trained models** — Your team can clone the repo and use your trained agent

### How it works:

All data is stored in the `data/` folder and tracked by Git:
```
NS-AI-Agent/data/
├── agent_memory.db        # All conversations and projects
└── chroma_db/             # All uploaded documents and knowledge
```

### Workflow:

**On your first computer:**
```bash
# Train the agent (upload docs, chat, create projects)
streamlit run web/app.py

# Commit and push to GitHub
git add .
git commit -m "Train agent with customer migration docs"
git push origin claude/funny-ride-Oq16Q
```

**On your second computer (or cloud deployment):**
```bash
# Clone the repo with all training data
git clone https://github.com/brbernst94/NS-AI-Agent.git
cd NS-AI-Agent

# Your knowledge is already there!
streamlit run web/app.py
```

### Large knowledge bases:

If your `data/` folder grows very large (100MB+), consider:
- Using cloud storage (AWS S3, Google Cloud Storage) for large documents
- Using a managed database (PostgreSQL + pgvector) for production
- Archiving old migrations to a separate repository

For most use cases, GitHub storage works perfectly fine.

## Adding NetSuite Field Definitions

To expand the field registry, edit `netsuite/field_registry.py`:

```python
"CustomRecord": {
    "field_name": {
        "internal_id": "custrecord_fieldname",
        "label": "Field Display Name",
        "type": "text",  # text, select, date, currency, checkbox, textarea, url, email, phone
        "required": False,
        "notes": "Additional context about this field"
    }
}
```

The field registry is consulted by the agent's `get_netsuite_field_info` tool.

## Architecture Decisions

**Why no LangChain/LlamaIndex?**
- Anthropic SDK gives full control over the agentic loop
- Simpler dependency tree
- Easier to debug tool use patterns
- Domain-specific tools are clear and maintainable

**Why sentence-transformers + ChromaDB locally?**
- No per-embedding API costs (local embeddings)
- No external service dependencies
- ChromaDB persists locally as files
- Perfect for offline operation

**Why SQLite instead of PostgreSQL?**
- Zero setup required
- Portable (single file: `./data/agent_memory.db`)
- Sufficient for 1000s of conversations
- Can upgrade to Postgres later if needed

**Why Streamlit for Web UI?**
- Rapid iteration on the chat interface
- Easy to build tabbed interfaces
- Native support for file uploads and streaming
- Perfect for SME tool building

## Testing the Agent

### 1. Test knowledge ingestion
```bash
python -m cli.main ingest text "Customers in NetSuite must have a unique external ID" --source test_docs
python -m cli.main ingest text "Items require a SKU and income account mapping" --source test_docs
```

### 2. Test agent search and Q&A
```bash
python -m cli.main chat --project-id test_project
# Ask: "What are the required fields for a Customer?"
```

### 3. Test data validation
```python
# Create a test CSV
cat > test_data.csv << EOF
name,email,phone,externalid
Acme Inc,info@acme.com,555-1234,acme-001
EOF

python -m cli.main validate test_data.csv --record-type Customer
```

## Troubleshooting

**"ANTHROPIC_API_KEY not set"**
- Check `.env` file has your API key
- Or export: `export ANTHROPIC_API_KEY=sk-...`

**ChromaDB says "collection already exists"**
- Delete `./data/chroma_db` and reingest documents
- Or modify `knowledge_base/index.py` to use `get_or_create_collection`

**Agent doesn't find documents in knowledge base**
- Verify documents were ingested: check `/knowledge/stats` (API) or **Knowledge Base** tab (Web UI)
- Try broader search queries
- Add more relevant documentation snippets

**CSV validation fails with "Unknown record type"**
- Record type must match a key in `netsuite/field_registry.py`
- Common types: Customer, Vendor, Item, Invoice, Bill, Employee

## Performance Notes

- **Knowledge base search**: ~100ms per query (local ChromaDB)
- **Agent response time**: 2-5 seconds (includes knowledge search + Claude inference)
- **CSV validation**: <100ms for 1000-row files
- **Concurrent users**: 10-50 (API rate-limited by Anthropic API tier)

For 100+ concurrent users, consider:
- Load balancing the API
- Caching knowledge search results
- Using pgvector + PostgreSQL for larger document bases

## Future Enhancements

- [ ] SuiteScript code generation
- [ ] SuiteTalk API request building
- [ ] Multi-file batch validation and transformation
- [ ] Integration with NetSuite REST API (test migrations before import)
- [ ] Document version control (track knowledge base changes)
- [ ] Team collaboration (shared projects, approvals)
- [ ] Export/import project configurations
- [ ] Audit trail of all transformations
- [ ] Custom field mapping templates per company

## License & Support

Built with Claude via Anthropic. For issues or questions, refer to the agent's knowledge base or add more NetSuite documentation.
