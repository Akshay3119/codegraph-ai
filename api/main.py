"""
==============================================================================
FastAPI Server — api/main.py
==============================================================================
Serving layer for the CodeGraph AI Codebase Analyzer.

Endpoints:
  POST /analyze              — Run a query through the LangGraph agent (sync).
  POST /analyze/stream       — SSE streaming version of /analyze.
  GET  /analyze/stream       — Browser-friendly SSE (query via query params).
  GET  /history/{thread_id}  — Retrieve checkpoint history for a thread.
  GET  /graph                — Return Neo4j nodes + relationships for viz.
  POST /ingest               — Trigger codebase ingestion into Neo4j + Qdrant.
  GET  /health               — Liveness check for all services.

Design Decisions:
  - The LangGraph is compiled once at startup with a SqliteSaver checkpointer
    and reused across requests via app.state.
  - All endpoints use Pydantic v2 models for request/response validation.
  - CORS is enabled for local frontend development.
  - Structured logging with request IDs for traceability.
==============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import model_validator
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ── Local imports ────────────────────────────────────────────────────────────
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402
from agent.graph import compile_graph, get_sqlite_checkpointer, GraphRAGState  # noqa: E402
from ingestion.parser import run_ingestion  # noqa: E402
from ingestion.github import clone_github_repository, parse_github_url  # noqa: E402

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("api.main")


# ══════════════════════════════════════════════════════════════════════════════
# Lifespan — Warm up the LangGraph on startup
# ══════════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    - On startup: create SqliteSaver checkpointer, compile LangGraph.
    - On shutdown: close the checkpointer connection.
    """
    logger.info("Initialising SqliteSaver checkpointer...")
    try:
        checkpointer = get_sqlite_checkpointer("checkpoints.db")
        app.state.checkpointer = checkpointer
        logger.info("SqliteSaver ready (checkpoints.db).")
    except ImportError as exc:
        logger.warning("SqliteSaver unavailable (%s). Running without checkpointing.", exc)
        checkpointer = None
        app.state.checkpointer = None

    logger.info("Compiling LangGraph state machine...")
    app.state.graph = compile_graph(checkpointer=checkpointer)
    logger.info("LangGraph compiled. Server ready.")

    yield

    logger.info("Shutting down...")
    if checkpointer is not None and hasattr(checkpointer, "conn"):
        try:
            checkpointer.conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="CodeGraph AI Codebase Analyzer",
    description=(
        "An agentic system that combines Knowledge Graphs (Neo4j) and "
        "Vector Search (Qdrant) with LLM-powered multi-agent orchestration "
        "(LangGraph) to answer complex questions about codebases."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# ── CORS — Allow local frontends ────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# Request / Response Models (Pydantic v2)
# ══════════════════════════════════════════════════════════════════════════════


class AnalyzeRequest(BaseModel):
    """Request body for the /analyze and /analyze/stream endpoints."""
    query: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Natural language question about the codebase.",
        examples=["What are the main classes in the auth module?"],
    )
    thread_id: Optional[str] = Field(
        default=None,
        description=(
            "Conversation thread identifier for checkpointed continuity. "
            "A UUID v4 is generated automatically when omitted."
        ),
    )


class AnalyzeResponse(BaseModel):
    """Response body for the /analyze endpoint."""
    request_id: str = Field(description="Unique identifier for this request.")
    thread_id: str = Field(description="Conversation thread identifier.")
    query: str = Field(description="The original user query.")
    strategy: str = Field(description="Retrieval strategy used (graph/vector/hybrid).")
    answer: str = Field(description="The synthesized answer from the agent.")
    graph_records_count: int = Field(description="Number of Neo4j records retrieved.")
    vector_chunks_count: int = Field(description="Number of Qdrant chunks retrieved.")
    retrieval_attempts: int = Field(description="Number of retrieval attempts (including retries).")
    latency_ms: float = Field(description="End-to-end processing time in milliseconds.")


class IngestRequest(BaseModel):
    """Request body for the /ingest endpoint."""
    codebase_path: Optional[str] = Field(
        default=None,
        description="Local path to the codebase to ingest.",
        examples=["./sample_codebase", "/home/user/my_project"],
    )
    github_url: Optional[str] = Field(
        default=None,
        description="Public GitHub repository URL to shallow-clone and ingest.",
        examples=[
            "https://github.com/fastapi/fastapi",
            "https://github.com/fastapi/fastapi/tree/master",
        ],
    )

    @model_validator(mode="after")
    def _validate_source(self) -> "IngestRequest":
        if self.codebase_path and self.github_url:
            raise ValueError("Provide either codebase_path or github_url, not both.")
        return self


class IngestResponse(BaseModel):
    """Response body for the /ingest endpoint."""
    request_id: str
    entities_parsed: int
    relationships_parsed: int
    target_path: str
    latency_ms: float
    source: str = Field(
        description="What was ingested: local path or GitHub URL.",
    )


class HealthResponse(BaseModel):
    """Response body for the /health endpoint."""
    status: str
    neo4j: str
    qdrant: str
    version: str


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════


def _build_initial_state(query: str) -> dict[str, Any]:
    """
    Input for a new turn. Only the new human message is sent; LangGraph's
    add_messages reducer appends it to the checkpointed thread history.
    Retrieval fields are reset so each turn runs fresh graph/vector search.
    """
    from langchain_core.messages import HumanMessage  # local to avoid top-level dep issues
    return {
        "messages": [HumanMessage(content=query)],
        "query_plan": {},
        "retrieved_graph_data": [],
        "retrieved_vector_data": [],
        "final_answer": "",
        "retrieval_attempts": 0,
    }


def _message_to_dict(msg: Any) -> Optional[dict[str, str]]:
    from langchain_core.messages import AIMessage, HumanMessage

    if isinstance(msg, HumanMessage):
        return {"role": "human", "content": str(msg.content)}
    if isinstance(msg, AIMessage):
        content = str(msg.content)
        # Skip internal agent trace messages in the UI
        if content.startswith("[") and "]" in content[:40]:
            return None
        return {"role": "ai", "content": content}
    return None


def _resolve_thread_id(thread_id: Optional[str]) -> str:
    return thread_id if thread_id else str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════


@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyze a codebase query",
    description=(
        "Runs a natural language query through the LangGraph multi-agent pipeline. "
        "The system automatically decides whether to use structural (Neo4j), "
        "semantic (Qdrant), or hybrid retrieval. Provide a thread_id to continue "
        "an existing conversation; omit it to start a new one."
    ),
)
async def analyze_codebase(request: AnalyzeRequest, fastapi_request: Request) -> AnalyzeResponse:
    """Main analysis endpoint (synchronous / blocking invocation)."""
    request_id = str(uuid.uuid4())[:8]
    thread_id = _resolve_thread_id(request.thread_id)
    start_time = time.perf_counter()

    logger.info(f"[{request_id}] thread={thread_id} query={request.query[:100]}...")

    try:
        graph = fastapi_request.app.state.graph
        initial_state = _build_initial_state(request.query)
        config = {"configurable": {"thread_id": thread_id}}

        final_state = graph.invoke(initial_state, config=config)

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        response = AnalyzeResponse(
            request_id=request_id,
            thread_id=thread_id,
            query=request.query,
            strategy=final_state.get("query_plan", {}).get("strategy", "unknown"),
            answer=final_state.get("final_answer", "No answer was generated."),
            graph_records_count=len(final_state.get("retrieved_graph_data", [])),
            vector_chunks_count=len(final_state.get("retrieved_vector_data", [])),
            retrieval_attempts=final_state.get("retrieval_attempts", 0),
            latency_ms=round(elapsed_ms, 2),
        )

        logger.info(
            f"[{request_id}] done in {elapsed_ms:.0f}ms | "
            f"strategy={response.strategy} graph={response.graph_records_count} "
            f"vector={response.vector_chunks_count}"
        )

        return response

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.error(f"[{request_id}] Error after {elapsed_ms:.0f}ms: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if "resource_exhausted" in str(e).lower() or "429" in str(e)
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_format_api_error(e),
        )


# ── SSE streaming helpers ────────────────────────────────────────────────────

def _iter_node_updates(chunk: Any) -> list[tuple[str, Any]]:
    """
    Normalize LangGraph stream chunks into (node_name, state_update) pairs.

    LangGraph v1+ yields a dict per step, e.g. ``{"query_analyzer": {...}}``.
    Older examples used two-tuples; v2 may emit typed parts with ``type``/``data``.
    """
    if isinstance(chunk, tuple):
        if len(chunk) == 2 and isinstance(chunk[0], str):
            return [(chunk[0], chunk[1])]
        if len(chunk) == 3 and isinstance(chunk[1], str):
            return [(chunk[1], chunk[2])]
        return []

    if not isinstance(chunk, dict):
        return []

    if chunk.get("type") == "updates":
        updates = chunk.get("data")
        if not isinstance(updates, dict):
            return []
    else:
        updates = chunk

    return [
        (node_name, state_update)
        for node_name, state_update in updates.items()
        if not str(node_name).startswith("__")
    ]


def _format_api_error(exc: Exception) -> str:
    """Turn provider errors (especially Gemini quota) into actionable messages."""
    msg = str(exc)
    lower = msg.lower()
    if "resource_exhausted" in lower or "429" in msg or "quota" in lower:
        retry_hint = ""
        m = re.search(r"retry in ([\d.]+)s", lower)
        if m:
            secs = max(1, int(float(m.group(1))))
            retry_hint = f" Short-term limit: retry in about {secs}s."
        return (
            "Gemini API quota exceeded for this model/key."
            f"{retry_hint} "
            "Free tier allows very few requests per day for gemini-2.5-flash; "
            "each CodeGraph AI query uses several LLM calls. "
            "Options: wait for the daily reset, enable billing in Google AI Studio, "
            "set GOOGLE_MODEL_NAME to another model in .env, or add GROQ_API_KEY for "
            "automatic Groq fallback (see .env.example). "
            "See https://ai.google.dev/gemini-api/docs/rate-limits"
        )
    if "api key" in lower or "invalid" in lower and "key" in lower:
        return "Invalid or missing GOOGLE_API_KEY. Set it in .env (see .env.example)."
    return msg


def _serialize_state_update(update: Any) -> str:
    """Convert a LangGraph stream update to a JSON-serialisable dict."""
    try:
        if isinstance(update, dict):
            # Strip non-serialisable objects (e.g. BaseMessage instances)
            safe = {}
            for k, v in update.items():
                try:
                    json.dumps(v, default=str)
                    safe[k] = v
                except Exception:
                    safe[k] = str(v)
            return json.dumps(safe, default=str)
        return json.dumps({"update": str(update)})
    except Exception as exc:
        return json.dumps({"error": f"serialization error: {exc}"})


async def _stream_graph(
    graph,
    initial_state: GraphRAGState,
    thread_id: str,
    request_id: str,
) -> AsyncGenerator[str, None]:
    """
    Async generator that streams LangGraph node outputs as SSE events.

    Each event carries a JSON payload: {"node": <name>, "state": <partial update>}.
    A final `[DONE]` sentinel is sent after the graph completes.
    """
    config = {"configurable": {"thread_id": thread_id}}

    # Node-friendly labels for the UI
    node_labels: dict[str, str] = {
        "query_analyzer": "🔍 Analyzing query...",
        "cypher_generation": "📊 Running Cypher query...",
        "vector_search": "🧲 Searching vector store...",
        "synthesizer": "🧠 Synthesizing answer...",
    }

    try:
        # stream_mode="updates" yields {node_name: state_update} dicts per step
        for chunk in graph.stream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            for node_name, state_update in _iter_node_updates(chunk):
                label = node_labels.get(node_name, f"⚙️ {node_name}")
                payload = {
                    "node": node_name,
                    "label": label,
                    "state": json.loads(_serialize_state_update(state_update)),
                }
                yield f"data: {json.dumps(payload)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as exc:
        logger.error(f"[{request_id}] Streaming error: {exc}", exc_info=True)
        yield f"data: {json.dumps({'error': _format_api_error(exc)})}\n\n"
        yield "data: [DONE]\n\n"


@app.post(
    "/analyze/stream",
    summary="Analyze a codebase query (SSE streaming)",
    description=(
        "Same as POST /analyze but streams each LangGraph node's output as "
        "Server-Sent Events. Final event is `data: [DONE]`."
    ),
)
async def analyze_stream(request: AnalyzeRequest, fastapi_request: Request) -> StreamingResponse:
    """SSE streaming analysis endpoint."""
    request_id = str(uuid.uuid4())[:8]
    thread_id = _resolve_thread_id(request.thread_id)

    logger.info(f"[{request_id}] SSE stream thread={thread_id} query={request.query[:100]}...")

    graph = fastapi_request.app.state.graph
    initial_state = _build_initial_state(request.query)

    return StreamingResponse(
        _stream_graph(graph, initial_state, thread_id, request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Thread-ID": thread_id,
            "X-Request-ID": request_id,
        },
    )


@app.get(
    "/analyze/stream",
    summary="Analyze a codebase query via SSE (browser-friendly)",
    description=(
        "Browser-friendly SSE endpoint — accepts query and thread_id as query "
        "parameters instead of a JSON body, making it easy to test with "
        "EventSource from the browser."
    ),
)
async def analyze_stream_get(
    fastapi_request: Request,
    query: str = Query(..., min_length=3, max_length=2000, description="Natural language question."),
    thread_id: Optional[str] = Query(default=None, description="Conversation thread ID."),
) -> StreamingResponse:
    """Browser-friendly SSE streaming endpoint."""
    request_id = str(uuid.uuid4())[:8]
    thread_id = _resolve_thread_id(thread_id)

    logger.info(f"[{request_id}] SSE-GET stream thread={thread_id} query={query[:100]}...")

    graph = fastapi_request.app.state.graph
    initial_state = _build_initial_state(query)

    return StreamingResponse(
        _stream_graph(graph, initial_state, thread_id, request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Thread-ID": thread_id,
            "X-Request-ID": request_id,
        },
    )


@app.get(
    "/history/{thread_id}",
    summary="Retrieve checkpoint history for a thread",
    description=(
        "Returns the stored checkpoint snapshots for the given thread_id. "
        "Requires the checkpointer to be enabled (SqliteSaver)."
    ),
)
async def get_thread_history(thread_id: str, fastapi_request: Request) -> dict:
    """Retrieve checkpoint metadata and the latest conversation state for a thread."""
    checkpointer = fastapi_request.app.state.checkpointer
    graph = fastapi_request.app.state.graph
    if checkpointer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkpointing is not enabled on this server.",
        )

    config = {"configurable": {"thread_id": thread_id}}
    history = []

    try:
        for checkpoint_tuple in checkpointer.list(config):
            checkpoint = checkpoint_tuple.checkpoint if hasattr(checkpoint_tuple, "checkpoint") else checkpoint_tuple
            metadata = checkpoint_tuple.metadata if hasattr(checkpoint_tuple, "metadata") else {}
            history.append({
                "checkpoint_id": checkpoint.get("id") if isinstance(checkpoint, dict) else str(checkpoint),
                "metadata": metadata,
            })
    except Exception as exc:
        logger.error(f"History fetch failed for thread {thread_id}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve history: {str(exc)}",
        )

    found = len(history) > 0
    messages: list[dict[str, str]] = []
    final_answer = ""
    human_turns = 0

    if found:
        try:
            snapshot = graph.get_state(config)
            values = snapshot.values if snapshot else {}
            for msg in values.get("messages") or []:
                item = _message_to_dict(msg)
                if item:
                    messages.append(item)
                    if item["role"] == "human":
                        human_turns += 1
            final_answer = str(values.get("final_answer") or "")
        except Exception as exc:
            logger.warning(f"Could not load state for thread {thread_id}: {exc}")

    return {
        "thread_id": thread_id,
        "found": found,
        "checkpoints": history,
        "count": len(history),
        "human_turns": human_turns,
        "messages": messages,
        "final_answer": final_answer,
    }


@app.delete(
    "/ingest",
    status_code=status.HTTP_200_OK,
    summary="Clear all ingested data",
    description="Deletes all nodes and relationships from Neo4j and all vectors from Qdrant.",
)
async def clear_ingested_data() -> dict:
    """Wipe Neo4j graph and Qdrant collection so a fresh codebase can be ingested."""
    from neo4j import GraphDatabase as _NeoDriver
    from qdrant_client import QdrantClient as _QdrantClient

    errors = []

    # Clear Neo4j
    try:
        driver = _NeoDriver.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        driver.close()
        logger.info("Neo4j cleared.")
    except Exception as exc:
        logger.error(f"Neo4j clear failed: {exc}", exc_info=True)
        errors.append(f"Neo4j: {exc}")

    # Clear Qdrant collection
    try:
        client = _QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        collections = [c.name for c in client.get_collections().collections]
        if settings.qdrant_collection_name in collections:
            client.delete_collection(settings.qdrant_collection_name)
            logger.info(f"Qdrant collection '{settings.qdrant_collection_name}' deleted.")
    except Exception as exc:
        logger.error(f"Qdrant clear failed: {exc}", exc_info=True)
        errors.append(f"Qdrant: {exc}")

    if errors:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Partial clear failure: " + "; ".join(errors),
        )

    return {"status": "cleared", "message": "All ingested data has been removed."}


@app.get(
    "/graph",
    summary="Fetch knowledge graph for visualization",
    description=(
        "Queries Neo4j for all nodes and relationships (up to the specified limit) "
        "and returns them in a format suitable for react-force-graph-2d."
    ),
)
async def get_graph_data(limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """Return Neo4j graph data as {nodes, links} for the frontend visualization."""
    from neo4j import GraphDatabase as _NeoDriver

    try:
        driver = _NeoDriver.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )

        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        node_ids_seen: set[int] = set()

        with driver.session() as session:
            # Fetch nodes
            node_records = session.run(
                f"MATCH (n) RETURN n, labels(n) AS lbls, id(n) AS nid LIMIT {limit}"
            )
            for record in node_records:
                nid = record["nid"]
                if nid in node_ids_seen:
                    continue
                node_ids_seen.add(nid)
                node = record["n"]
                props = dict(node)
                lbls = record["lbls"]
                raw_props = {k: str(v) for k, v in props.items()}
                if "file_path" in raw_props:
                    fp = raw_props["file_path"]
                    raw_props["file_path"] = fp.split("/")[-1].split("\\")[-1]
                nodes.append({
                    "id": str(nid),
                    "label": props.get("qualified_name", props.get("name", str(nid))),
                    "type": lbls[0] if lbls else "Unknown",
                    "properties": raw_props,
                })

            # Fetch relationships
            rel_records = session.run(
                f"MATCH (a)-[r]->(b) RETURN id(a) AS src, id(b) AS tgt, type(r) AS rtype "
                f"LIMIT {limit}"
            )
            for record in rel_records:
                links.append({
                    "source": str(record["src"]),
                    "target": str(record["tgt"]),
                    "type": record["rtype"],
                })

        driver.close()
        return {"nodes": nodes, "links": links}

    except Exception as exc:
        logger.error(f"GET /graph error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch graph data: {str(exc)}",
        )


@app.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a codebase",
    description=(
        "Parse a Python codebase and ingest entities into Neo4j and Qdrant. "
        "Provide a local `codebase_path` or a public `github_url`."
    ),
)
async def ingest_codebase(request: IngestRequest) -> IngestResponse:
    """Ingestion endpoint — triggers the full parse → Neo4j → Qdrant pipeline."""
    request_id = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()
    source_label: str

    try:
        if request.github_url:
            ref = parse_github_url(request.github_url)
            source_label = ref.canonical_url
            logger.info(f"[{request_id}] Cloning GitHub repo: {source_label}")
            target = await asyncio.to_thread(
                clone_github_repository,
                request.github_url,
                Path(settings.github_clone_root),
            )
        else:
            target = Path(request.codebase_path or settings.target_codebase_path)
            source_label = str(target)

        logger.info(f"[{request_id}] Ingestion requested for: {target}")

        summary = await asyncio.to_thread(run_ingestion, str(target))
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return IngestResponse(
            request_id=request_id,
            entities_parsed=summary["entities_parsed"],
            relationships_parsed=summary["relationships_parsed"],
            target_path=summary["target_path"],
            latency_ms=round(elapsed_ms, 2),
            source=source_label,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"[{request_id}] Ingestion failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {str(e)}",
        )


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Check connectivity to Neo4j and Qdrant.",
)
async def health_check() -> HealthResponse:
    """Liveness probe — verifies that both Neo4j and Qdrant are reachable."""
    neo4j_status = "unknown"
    qdrant_status = "unknown"

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        with driver.session() as session:
            session.run("RETURN 1")
        driver.close()
        neo4j_status = "healthy"
    except Exception as e:
        neo4j_status = f"unhealthy: {e}"

    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        client.get_collections()
        qdrant_status = "healthy"
    except Exception as e:
        qdrant_status = f"unhealthy: {e}"

    overall = "healthy" if "healthy" == neo4j_status == qdrant_status else "degraded"

    return HealthResponse(
        status=overall,
        neo4j=neo4j_status,
        qdrant=qdrant_status,
        version="0.2.0",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Entry point — run with: python -m api.main
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
