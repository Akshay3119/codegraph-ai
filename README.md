# CodeGraph AI Codebase Analyzer

> **Stateful multi-agent orchestration** combining Knowledge Graphs (Neo4j) + Vector Search (Qdrant) + LLM-powered reasoning (LangGraph) to answer complex questions about codebases — with a real-time streaming Next.js frontend.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Next.js Frontend (port 3000)                       │
│                                                                             │
│  ┌──────────────┐  ┌───────────────────┐  ┌──────────────────────────────┐ │
│  │ IngestPanel   │  │ StreamingAnswer   │  │   GraphVisualization         │ │
│  │ (POST /ingest)│  │ (SSE /analyze/    │  │   (react-force-graph-2d)    │ │
│  │               │  │       stream)     │  │   (GET /graph)              │ │
│  └──────────────┘  └───────────────────┘  └──────────────────────────────┘ │
└─────────────────────────────────────────┬───────────────────────────────────┘
                                          │ HTTP / SSE
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FastAPI Server (port 8000)                           │
│                                                                             │
│   POST /analyze          ─ Synchronous agent invocation                     │
│   POST /analyze/stream   ─ SSE streaming (node-by-node updates)             │
│   GET  /analyze/stream   ─ Browser-friendly SSE (query params)              │
│   GET  /history/{tid}    ─ Checkpoint history for a thread                  │
│   GET  /graph            ─ Neo4j nodes + links for visualization            │
│   POST /ingest           ─ Trigger codebase ingestion pipeline              │
│   GET  /health           ─ Liveness check (Neo4j + Qdrant)                  │
│                                                                             │
│   Lifespan: SqliteSaver checkpointer + compiled LangGraph on startup        │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       LangGraph State Machine                               │
│                                                                             │
│   ┌──────────────┐                                                          │
│   │  User Query   │                                                         │
│   └──────┬───────┘                                                          │
│          ▼                                                                  │
│   ┌──────────────────┐                                                      │
│   │  Query Analyzer   │  ← Classifies intent → {graph, vector, hybrid}      │
│   └──────┬───────────┘                                                      │
│          │                                                                  │
│          ├── "graph"  ──► ┌──────────────────┐                              │
│          │                │ Cypher Generator  │ → Neo4j                     │
│          │                └──────┬───────────┘                              │
│          │                       │ (if hybrid) ──► ┐                        │
│          │                                         │                        │
│          ├── "vector" ──► ┌──────────────────┐     │                        │
│          │                │  Vector Searcher  │ ◄──┘                        │
│          │                └──────┬───────────┘                              │
│          │                       │                                          │
│          └── "hybrid" ──► Cypher → Vector (sequential)                      │
│                                  │                                          │
│                                  ▼                                          │
│                     ┌───────────────────────┐                               │
│                     │  Synthesizer / Critic  │ ← Blends data, checks refs   │
│                     └───────────┬───────────┘                               │
│                                 │                                           │
│                          ┌──────┴──────┐                                    │
│                          │  Has code   │                                    │
│                          │ citations?  │                                    │
│                          └──────┬──────┘                                    │
│                            YES  │  NO (& retries ≤ max_retrieval_retries)   │
│                             ▼   ▼                                           │
│                           END  Loop back to retrieval                       │
│                                                                             │
│   State: GraphRAGState (TypedDict) with add_messages reducer               │
│   Checkpoint: SqliteSaver (checkpoints.db) for thread continuity            │
└──────────────┬──────────────────────────────┬───────────────────────────────┘
               │                              │
               ▼                              ▼
┌─────────────────────┐          ┌──────────────────────┐
│       Neo4j 5       │          │       Qdrant         │
│  Knowledge Graph    │          │     Vector DB        │
│                     │          │                      │
│  Nodes:             │          │  Collection:         │
│   :Module           │          │   codebase_chunks    │
│   :Class            │          │                      │
│   :Function         │          │  Points per entity:  │
│                     │          │   • docstring chunk   │
│  Relationships:     │          │   • source code chunk │
│   [:IMPORTS]        │          │                      │
│   [:DEFINES]        │          │  Payload:            │
│   [:CALLS]          │          │   qualified_name,    │
│                     │          │   entity_type,       │
│  Properties:        │          │   file_path,         │
│   qualified_name    │          │   start_line,        │
│   file_path         │          │   end_line,          │
│   start_line        │          │   text               │
│   end_line          │          │                      │
│   docstring_preview │          │  Distance: Cosine    │
│   entity_type       │          │  Dim: 1536 (default) │
│   external (bool)   │          │                      │
└─────────────────────┘          └──────────────────────┘
```

---

## Quick Start

### 1. Start Infrastructure
```bash
docker-compose up -d
```
This spins up:
- **Neo4j 5 Community** — `bolt://localhost:7687` (browser: `http://localhost:7474`, creds: `neo4j/graphrag2024`)
- **Qdrant v1.12** — `http://localhost:6333` (dashboard: `http://localhost:6333/dashboard`)

### 2. Install Python Dependencies
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
# Edit .env with your API keys
```

### 4. Ingest a Codebase
```bash
python -m ingestion.parser ./sample_codebase
```

### 5. Run the API Server
```bash
python -m api.main
# Server at http://localhost:8000
# Docs at http://localhost:8000/docs
```

### 6. Run the Frontend
```bash
cd frontend
npm install
npm run dev
# UI at http://localhost:3000
```

### 7. Query
```bash
# Sync
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"query": "What classes are defined in the auth module and what methods do they have?"}'

# SSE Streaming
curl -N http://localhost:8000/analyze/stream?query=What+classes+are+in+auth%3F
```

---

## Project Structure

```
CodeGraph AI/
├── docker-compose.yml              # Neo4j + Qdrant local infrastructure
├── requirements.txt                # Python dependencies
├── config.py                       # Pydantic-settings centralized config
├── .env.example                    # Environment variable template
│
├── ingestion/                      # Stage 1: Data Engineering
│   ├── __init__.py
│   └── parser.py                   # AST parser → Neo4j + Qdrant ingestion
│       ├── CodebaseASTVisitor      #   ast.NodeVisitor extracting entities & rels
│       ├── Neo4jWriter             #   MERGE-based idempotent graph writes
│       ├── QdrantWriter            #   Batch embedding + upsert
│       └── run_ingestion()         #   Orchestrates Parse → Neo4j → Qdrant
│
├── agent/                          # Stage 2: LangGraph Agent
│   ├── __init__.py                 #   Exports: compile_graph, get_compiled_graph
│   └── graph.py                    #   LangGraph state machine (core engine)
│       ├── GraphRAGState           #   TypedDict with add_messages reducer
│       ├── query_analyzer_node     #   Classifies → graph/vector/hybrid
│       ├── cypher_generation_node  #   LLM → Cypher → Neo4j execution
│       ├── vector_search_node      #   Embed query → Qdrant search
│       ├── synthesizer_node        #   Blends data, critic loop
│       ├── route_after_analysis    #   Conditional edge: strategy routing
│       ├── route_after_cypher      #   Conditional edge: hybrid chaining
│       ├── route_after_synthesis   #   Conditional edge: self-correction
│       └── build_graph / compile   #   StateGraph construction & Pregel compile
│
├── api/                            # Stage 3: Serving Layer
│   ├── __init__.py
│   └── main.py                     # FastAPI endpoints (7 routes)
│       ├── POST /analyze           #   Synchronous agent invocation
│       ├── POST /analyze/stream    #   SSE streaming (node-by-node)
│       ├── GET  /analyze/stream    #   Browser-friendly SSE
│       ├── GET  /history/{tid}     #   Checkpoint history
│       ├── GET  /graph             #   Knowledge graph data for viz
│       ├── POST /ingest            #   Trigger ingestion pipeline
│       └── GET  /health            #   Liveness check
│
├── frontend/                       # Stage 4: Next.js UI
│   ├── app/
│   │   ├── layout.tsx              #   Root layout (Geist fonts, TailwindCSS 4)
│   │   ├── page.tsx                #   Main page — query input + results
│   │   ├── globals.css             #   Global styles
│   │   └── components/
│   │       ├── GraphVisualization.tsx   # react-force-graph-2d knowledge graph
│   │       ├── StreamingAnswer.tsx      # SSE pipeline steps + markdown answer
│   │       └── IngestPanel.tsx          # Codebase ingestion trigger
│   └── package.json                #   Next.js 16, React 19, TailwindCSS 4
│
└── sample_codebase/                # Demo codebase for ingestion testing
    ├── auth.py                     #   User, AuthService (token-based auth)
    └── data_processing.py          #   DataLoader, transform_records, validate
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | Next.js 16, React 19, TailwindCSS 4 | Real-time streaming UI with SSE |
| **Graph Viz** | react-force-graph-2d | Interactive knowledge graph rendering |
| **Markdown** | react-markdown | Rendered agent responses |
| **Data Fetching** | SWR | Client-side graph data fetching with caching |
| **Orchestration** | LangGraph ≥ 0.2 | Stateful multi-agent routing with conditional edges |
| **Checkpointing** | langgraph-checkpoint-sqlite | Conversation persistence via SqliteSaver |
| **Knowledge Graph** | Neo4j 5 Community | Structural relationships (IMPORTS, CALLS, DEFINES) |
| **Vector DB** | Qdrant v1.12 | Semantic search over code chunks and docstrings |
| **Backend** | FastAPI ≥ 0.115 | REST API + SSE streaming with Pydantic v2 validation |
| **LLM** | langchain-openai | Provider-agnostic (swap to Groq/local via base_url) |
| **Embeddings** | OpenAIEmbeddings | text-embedding-3-small (1536-dim, configurable) |
| **Parsing** | Python `ast` | Zero-dependency structural code analysis |
| **Config** | pydantic-settings | Typed settings from env vars / `.env` |
| **Containerization** | Docker Compose | Neo4j + Qdrant local infrastructure |

---

## Agent State Machine

The core engine is a **LangGraph StateGraph** implementing the Supervisor multi-agent routing pattern.

### State Schema (`GraphRAGState`)

| Field | Type | Description |
|-------|------|-------------|
| `messages` | `list[BaseMessage]` | Conversation history (append-only via `add_messages` reducer) |
| `query_plan` | `dict` | Query Analyzer output: `{strategy, graph_query_hint, semantic_query}` |
| `retrieved_graph_data` | `list[dict]` | Cypher query results from Neo4j |
| `retrieved_vector_data` | `list[dict]` | Semantic search results from Qdrant |
| `final_answer` | `str` | The synthesized response |
| `retrieval_attempts` | `int` | Counter for the self-correction loop |

### Node Functions

| Node | Responsibility |
|------|---------------|
| **Query Analyzer** | Classifies user intent → `graph`, `vector`, or `hybrid` strategy via LLM |
| **Cypher Generator** | LLM generates Cypher from natural language → executes against Neo4j |
| **Vector Searcher** | Embeds semantic query → top-10 Qdrant nearest-neighbor search |
| **Synthesizer / Critic** | Blends both data sources into a cited markdown answer; triggers re-retrieval if `[INSUFFICIENT_DATA]` |

### Routing Logic

| Edge | Condition | Target |
|------|-----------|--------|
| After Analysis | `strategy == "graph"` | Cypher Generator |
| After Analysis | `strategy == "vector"` | Vector Searcher |
| After Analysis | `strategy == "hybrid"` | Cypher Generator → Vector Searcher (sequential) |
| After Cypher | `strategy == "hybrid"` | Vector Searcher |
| After Cypher | `strategy == "graph"` | Synthesizer |
| After Synthesis | Answer has citations | END |
| After Synthesis | `[INSUFFICIENT_DATA]` & retries ≤ max | Loop back to retrieval |

---

## Ingestion Pipeline

The `ingestion/parser.py` module runs three stages:

```
     ┌─────────┐        ┌─────────┐        ┌─────────┐
     │ Stage 1  │  ───►  │ Stage 2  │  ───►  │ Stage 3  │
     │ AST      │        │ Neo4j    │        │ Qdrant   │
     │ Parsing  │        │ Writes   │        │ Embeds   │
     └─────────┘        └─────────┘        └─────────┘
```

1. **AST Parsing** — `CodebaseASTVisitor` walks each `.py` file:
   - Extracts **entities**: modules, classes, functions (with docstrings + source)
   - Extracts **relationships**: IMPORTS, DEFINES, CALLS
   - Handles async functions, chained attribute calls, star imports

2. **Neo4j Ingestion** — `Neo4jWriter`:
   - MERGE-based idempotent writes (safe for re-runs)
   - Creates uniqueness constraints on `qualified_name` per label
   - Tags unresolved external dependencies with `external = true`

3. **Qdrant Ingestion** — `QdrantWriter`:
   - Each entity → up to 2 points (docstring chunk + source code chunk)
   - Deterministic UUIDs (MD5-based) for idempotent upserts
   - Batched embedding calls (configurable `batch_size`, default 64)

---

## API Endpoints

| Method | Path | Description | Request | Response |
|--------|------|-------------|---------|----------|
| `POST` | `/analyze` | Synchronous agent query | `{query, thread_id?}` | `AnalyzeResponse` (answer, strategy, counts, latency) |
| `POST` | `/analyze/stream` | SSE streaming query | `{query, thread_id?}` | `text/event-stream` — per-node updates, final `[DONE]` |
| `GET` | `/analyze/stream` | Browser-friendly SSE | `?query=...&thread_id=...` | Same as POST stream |
| `GET` | `/history/{thread_id}` | Checkpoint history | — | `{thread_id, checkpoints[], count}` |
| `GET` | `/graph` | Knowledge graph data | `?limit=200` | `{nodes[], links[]}` for react-force-graph |
| `POST` | `/ingest` | Trigger ingestion | `{codebase_path?}` | `IngestResponse` (entities, rels, latency) |
| `GET` | `/health` | Liveness probe | — | `{status, neo4j, qdrant, version}` |

---

## Frontend Components

| Component | File | Description |
|-----------|------|-------------|
| **Home Page** | `app/page.tsx` | Query input (textarea + ⌘↵ submit), thread management, sidebar layout |
| **StreamingAnswer** | `components/StreamingAnswer.tsx` | Displays pipeline step progress (emoji labels, color-coded nodes) + renders final answer as markdown |
| **GraphVisualization** | `components/GraphVisualization.tsx` | Interactive force-directed graph (SWR data fetch, color-coded by type, node detail sidebar on click) |
| **IngestPanel** | `components/IngestPanel.tsx` | Sidebar widget to trigger ingestion with custom path, shows entity/relationship counts |

---

## Configuration

All settings are centralized in `config.py` using `pydantic-settings` and read from environment variables or `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | `sk-placeholder` | LLM provider API key |
| `OPENAI_API_BASE` | `None` | Override base URL for Groq/vLLM/Ollama |
| `OPENAI_MODEL_NAME` | `gpt-4o-mini` | Chat model identifier |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `EMBEDDING_DIMENSION` | `1536` | Vector dimensionality |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `graphrag2024` | Neo4j password |
| `QDRANT_HOST` | `localhost` | Qdrant host |
| `QDRANT_PORT` | `6333` | Qdrant REST port |
| `QDRANT_COLLECTION_NAME` | `codebase_chunks` | Qdrant collection |
| `TARGET_CODEBASE_PATH` | `./sample_codebase` | Default ingestion target |
| `CHUNK_MAX_TOKENS` | `512` | Max token count per chunk |
| `MAX_RETRIEVAL_RETRIES` | `2` | Self-correction loop limit |

---

## LLM Provider Swapping

The architecture is designed for zero-code provider swaps via environment variables:

```bash
# OpenAI (default)
OPENAI_API_KEY=sk-...
OPENAI_MODEL_NAME=gpt-4o-mini

# Groq
OPENAI_API_BASE=https://api.groq.com/openai/v1
OPENAI_API_KEY=gsk_...
OPENAI_MODEL_NAME=llama3-70b-8192

# Local (vLLM / Ollama)
OPENAI_API_BASE=http://localhost:8080/v1
OPENAI_API_KEY=dummy
OPENAI_MODEL_NAME=meta-llama/Llama-3-8b
```

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **TypedDict state** (not Pydantic) | LangGraph convention — mutable dict merging per node |
| **Pure function nodes** | `(state) → partial update` — composable, testable, no side-effect coupling |
| **MERGE-based Neo4j writes** | Idempotent ingestion — safe for repeated re-runs |
| **Deterministic Qdrant IDs** | MD5-based UUIDs prevent duplicate points on re-ingestion |
| **Conditional edges** (not hardcoded chains) | Flexible routing; strategy is decided at runtime by the LLM |
| **SqliteSaver checkpointing** | Conversation continuity across requests via `thread_id` |
| **SSE streaming** | Real-time per-node pipeline visibility in the frontend |
| **Provider-agnostic LLM** | Single env var swap to move between OpenAI, Groq, or local models |
| **`from __future__ import annotations`** | Enables `str | None` union syntax across Python 3.9+ |
