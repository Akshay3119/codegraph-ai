"""
==============================================================================
CodeGraph AI — LangGraph State Machine (agent/graph.py)
==============================================================================
This is the **core orchestration engine** — a compiled LangGraph StateGraph
implementing the Supervisor multi-agent routing pattern.

Architecture Overview:
┌──────────────┐
│  User Query  │
└──────┬───────┘
       ▼
┌──────────────────┐
│  Query Analyzer  │  ← Classifies intent → {graph, vector, hybrid}
└──────┬───────────┘
       │
       ├── "graph"   ──► ┌──────────────────┐
       │                 │ Cypher Generator  │ → Neo4j → state.retrieved_graph_data
       │                 └──────────────────┘
       │
       ├── "vector"  ──► ┌──────────────────┐
       │                 │  Vector Searcher  │ → Qdrant → state.retrieved_vector_data
       │                 └──────────────────┘
       │
       └── "hybrid"  ──► Both nodes execute sequentially
                                │
                                ▼
                    ┌───────────────────────┐
                    │  Synthesizer / Critic │  ← Blends data, checks citations
                    └───────────┬───────────┘
                                │
                         ┌──────┴──────┐
                         │ Has citations│
                         │  in answer? │
                         └──────┬──────┘
                           YES  │  NO (& retries left)
                            ▼   ▼
                         END   Loop back to retrieval

Design Decisions:
  - State is a TypedDict (not a Pydantic model) per LangGraph convention.
  - Each node is a pure function: (state) → partial state update.
  - The graph compiles to a Pregel-based execution engine with checkpointing.
  - LLM calls use langchain_openai.ChatOpenAI for provider-agnostic swaps.
  - Conditional edges handle routing; no hardcoded chains.
==============================================================================
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal, TypedDict, Optional, Union

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from neo4j import GraphDatabase
from qdrant_client import QdrantClient

# SqliteSaver: try the standalone package first (langgraph >= 0.2.x uses
# langgraph-checkpoint-sqlite), fall back to the built-in path for older builds.
try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pragma: no cover
    try:
        from langgraph_checkpoint_sqlite import SqliteSaver  # type: ignore[no-redef]
    except ImportError:
        SqliteSaver = None  # type: ignore[assignment,misc]

# ── Local imports ────────────────────────────────────────────────────────────
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402
from agent.llm import invoke_llm  # noqa: E402

logger = logging.getLogger("agent.graph")


# ══════════════════════════════════════════════════════════════════════════════
# 1. STATE DEFINITION
# ══════════════════════════════════════════════════════════════════════════════


class GraphRAGState(TypedDict):
    """
    Shared state flowing through the LangGraph execution.

    Fields:
        messages: Conversation history (LangGraph's `add_messages` reducer
                  appends new messages instead of overwriting).
        query_plan: Output from the Query Analyzer — dict with keys:
                    {"strategy": "graph"|"vector"|"hybrid",
                     "graph_query_hint": str,
                     "semantic_query": str}
        retrieved_graph_data: Cypher query results from Neo4j.
        retrieved_vector_data: Semantic search results from Qdrant.
        final_answer: The synthesized response string.
        retrieval_attempts: Counter for self-correction loop.
    """
    messages: Annotated[list[BaseMessage], add_messages]
    query_plan: dict[str, Any]
    retrieved_graph_data: list[dict[str, Any]]
    retrieved_vector_data: list[dict[str, Any]]
    final_answer: str
    retrieval_attempts: int


# ══════════════════════════════════════════════════════════════════════════════
# 2. SHARED RESOURCES (initialized once, reused across invocations)
# ══════════════════════════════════════════════════════════════════════════════


def _build_embeddings() -> GoogleGenerativeAIEmbeddings:
    """Construct the Google embedding model."""
    return GoogleGenerativeAIEmbeddings(
        model=settings.google_embedding_model,
        google_api_key=settings.google_api_key,
        output_dimensionality=settings.embedding_dimension,
    )


def _get_neo4j_driver():
    """Lazy Neo4j driver constructor."""
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
    )


def _get_qdrant_client():
    """Lazy Qdrant client constructor."""
    return QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


# ══════════════════════════════════════════════════════════════════════════════
# 3. NODE IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════
#
# Each node is a function with signature:
#   (state: GraphRAGState) → dict   (partial state update)
#
# LangGraph merges the returned dict into the current state.
# ══════════════════════════════════════════════════════════════════════════════


# ── 3A. Query Analyzer ──────────────────────────────────────────────────────

QUERY_ANALYZER_SYSTEM_PROMPT = """\
You are a query analyzer for a codebase knowledge system. You have access to:
1. A **Knowledge Graph** (Neo4j) storing structural relationships: which files
   define which classes/functions, what imports what, what calls what.
2. A **Vector Database** (Qdrant) storing embedded docstrings and source code
   for semantic similarity search.

Given the user's question, decide the best retrieval strategy:
- "graph"  → The question is about **structure** (e.g., "What classes does module X define?",
             "Show the call graph for function Y", "What imports Z?").
- "vector" → The question is about **semantics** (e.g., "Find code related to authentication",
             "Which functions handle error logging?").
- "hybrid" → The question requires **both** structural and semantic data
             (e.g., "Explain how the auth module works and what calls it").

Respond with ONLY a JSON object (no markdown fences, no extra text):
{
  "strategy": "graph" | "vector" | "hybrid",
  "graph_query_hint": "A natural-language description of what to look up in the graph (empty string if not needed)",
  "semantic_query": "The semantic search query to embed (empty string if not needed)"
}
"""


def query_analyzer_node(state: GraphRAGState) -> dict:
    """
    Node: Analyze the user query and produce a retrieval plan.

    Takes the latest user message, asks the LLM to classify it, and writes
    the structured plan to state["query_plan"].
    """
    # Extract the last human message from the conversation
    user_query = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            user_query = msg.content
            break

    if not user_query:
        return {
            "query_plan": {
                "strategy": "vector",
                "graph_query_hint": "",
                "semantic_query": "general codebase overview",
            },
            "messages": [AIMessage(content="[Analyzer] No user query found, defaulting to vector search.")],
        }

    response = invoke_llm([
        SystemMessage(content=QUERY_ANALYZER_SYSTEM_PROMPT),
        HumanMessage(content=f"User question: {user_query}"),
    ])

    # Parse the JSON response, with fallback
    try:
        # Strip potential markdown code fences the LLM might add despite instructions
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        plan = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        logger.warning(f"Failed to parse query plan, falling back to hybrid. Raw: {response.content}")
        plan = {
            "strategy": "hybrid",
            "graph_query_hint": user_query,
            "semantic_query": user_query,
        }

    # Validate and ensure required keys
    plan.setdefault("strategy", "hybrid")
    plan.setdefault("graph_query_hint", "")
    plan.setdefault("semantic_query", user_query)

    logger.info(f"[QueryAnalyzer] Strategy: {plan['strategy']}")

    return {
        "query_plan": plan,
        "messages": [
            AIMessage(content=f"[Analyzer] Strategy: {plan['strategy']}. Plan: {json.dumps(plan)}")
        ],
        "retrieval_attempts": state.get("retrieval_attempts", 0),
    }


# ── 3B. Cypher Generation & Execution Node ──────────────────────────────────

CYPHER_GENERATION_SYSTEM_PROMPT = """\
You are a Cypher query generator for a Neo4j codebase knowledge graph.

The graph schema:
  Node Labels: Module, Class, Function (all have property `qualified_name`)
  Node Properties: qualified_name, file_path, start_line, end_line, docstring_preview, entity_type
  Relationship Types: IMPORTS, DEFINES, CALLS
  External nodes may have property `external = true`.

Given the user's structural question, generate a Cypher query to retrieve the
relevant subgraph. Return ONLY the Cypher query string, no explanation, no
markdown fences.

Guidelines:
- Use MATCH patterns, not OPTIONAL MATCH, unless explicitly needed.
- LIMIT results to 25 to avoid overwhelming the synthesizer.
- Return node properties, not raw nodes, for serialization safety.
- Use case-insensitive matching with `toLower()` for name searches.
"""


def cypher_generation_node(state: GraphRAGState) -> dict:
    """
    Node: Generate a Cypher query from the query plan, execute it against
    Neo4j, and write results to state["retrieved_graph_data"].
    """
    plan = state.get("query_plan", {})
    hint = plan.get("graph_query_hint", "")

    if not hint:
        logger.info("[CypherGen] No graph query hint, skipping.")
        return {
            "retrieved_graph_data": [],
            "messages": [AIMessage(content="[CypherGen] No graph query needed.")],
        }

    # Ask the LLM to generate Cypher
    response = invoke_llm([
        SystemMessage(content=CYPHER_GENERATION_SYSTEM_PROMPT),
        HumanMessage(content=f"User's structural question: {hint}"),
    ])

    cypher_query = response.content.strip()
    # Strip markdown fences if present
    cypher_query = re.sub(r"^```(?:cypher)?\s*", "", cypher_query)
    cypher_query = re.sub(r"\s*```$", "", cypher_query)

    logger.info(f"[CypherGen] Generated Cypher:\n{cypher_query}")

    # Execute against Neo4j
    driver = _get_neo4j_driver()
    results: list[dict[str, Any]] = []
    try:
        with driver.session() as session:
            records = session.run(cypher_query)
            for record in records:
                results.append(dict(record))
    except Exception as e:
        logger.error(f"[CypherGen] Cypher execution error: {e}")
        results = [{"error": str(e), "cypher": cypher_query}]
    finally:
        driver.close()

    # Serialize Neo4j results — convert Node/Relationship objects to dicts
    serialized = _serialize_neo4j_results(results)
    logger.info(f"[CypherGen] Retrieved {len(serialized)} records from Neo4j")

    return {
        "retrieved_graph_data": serialized,
        "messages": [
            AIMessage(
                content=f"[CypherGen] Executed Cypher query. Retrieved {len(serialized)} records."
            )
        ],
    }


def _serialize_neo4j_results(results: list[dict]) -> list[dict[str, Any]]:
    """
    Convert Neo4j result records to JSON-serializable dicts.
    Neo4j driver returns Node/Relationship objects that aren't directly
    serializable — we extract their properties.
    """
    serialized = []
    for record in results:
        clean_record = {}
        for key, value in record.items():
            if hasattr(value, "items"):
                # It's a dict-like object (Node properties)
                clean_record[key] = dict(value)
            elif hasattr(value, "_properties"):
                # Neo4j Node or Relationship object
                clean_record[key] = dict(value._properties)
            elif isinstance(value, list):
                clean_record[key] = [
                    dict(v._properties) if hasattr(v, "_properties") else v
                    for v in value
                ]
            else:
                clean_record[key] = value
        serialized.append(clean_record)
    return serialized


# ── 3C. Vector Search Node ──────────────────────────────────────────────────


def vector_search_node(state: GraphRAGState) -> dict:
    """
    Node: Embed the semantic query, search Qdrant, and write results
    to state["retrieved_vector_data"].
    """
    plan = state.get("query_plan", {})
    semantic_query = plan.get("semantic_query", "")

    if not semantic_query:
        logger.info("[VectorSearch] No semantic query, skipping.")
        return {
            "retrieved_vector_data": [],
            "messages": [AIMessage(content="[VectorSearch] No semantic query needed.")],
        }

    # Embed the query
    embeddings = _build_embeddings()
    try:
        query_vector = embeddings.embed_query(semantic_query)
    except Exception as e:
        logger.error(f"[VectorSearch] Embedding failed: {e}")
        return {
            "retrieved_vector_data": [],
            "messages": [AIMessage(content=f"[VectorSearch] Embedding error: {e}")],
        }

    # Search Qdrant (qdrant-client >=1.7 uses query_points, not search)
    client = _get_qdrant_client()
    try:
        response = client.query_points(
            collection_name=settings.qdrant_collection_name,
            query=query_vector,
            limit=10,
            with_payload=True,
        )
        search_results = response.points
    except Exception as e:
        logger.error(f"[VectorSearch] Qdrant search failed: {e}")
        return {
            "retrieved_vector_data": [],
            "messages": [AIMessage(content=f"[VectorSearch] Search error: {e}")],
        }

    # Extract payload + score into serializable dicts
    vector_results = []
    for hit in search_results:
        payload = hit.payload or {}
        entry = {
            "score": hit.score,
            **payload,
        }
        vector_results.append(entry)

    logger.info(f"[VectorSearch] Retrieved {len(vector_results)} chunks from Qdrant")

    return {
        "retrieved_vector_data": vector_results,
        "messages": [
            AIMessage(
                content=f"[VectorSearch] Found {len(vector_results)} relevant code chunks."
            )
        ],
    }


# ── 3D. Synthesizer / Critic Node ──────────────────────────────────────────

SYNTHESIZER_SYSTEM_PROMPT = """\
You are a senior software engineer analyzing a codebase. You have been given:
1. **Structural data** from a knowledge graph (call graphs, imports, class hierarchies).
2. **Semantic data** from a vector search (relevant code snippets and docstrings).

Your task:
- Synthesize a clear, comprehensive answer to the user's question.
- ALWAYS include **code citations**: reference specific file paths, function/class names,
  and line numbers when available.
- If the retrieved data is insufficient, say so explicitly and suggest what additional
  information might help.
- Format your answer in clean markdown with code blocks where appropriate.

CRITICAL: Your answer MUST contain at least one specific code reference
(file path, function name, or qualified name) from the retrieved data.
If you cannot find any relevant code references, start your answer with
"[INSUFFICIENT_DATA]" to trigger a re-retrieval.
"""


def synthesizer_node(state: GraphRAGState) -> dict:
    """
    Node: Blend graph and vector data into a final answer.
    Implements a critic check — if the answer lacks citations, it signals
    for re-retrieval via the "[INSUFFICIENT_DATA]" prefix.
    """
    # Gather context from both retrieval sources
    graph_data = state.get("retrieved_graph_data", [])
    vector_data = state.get("retrieved_vector_data", [])

    # Build context string for the LLM
    context_parts = []

    if graph_data:
        context_parts.append("## Structural Data (Knowledge Graph)\n")
        for i, record in enumerate(graph_data[:15], 1):  # Cap to avoid token overflow
            context_parts.append(f"**Record {i}:** {json.dumps(record, default=str, indent=2)}\n")

    if vector_data:
        context_parts.append("\n## Semantic Data (Vector Search)\n")
        for i, chunk in enumerate(vector_data[:10], 1):
            text_preview = chunk.get("text", "")[:500]
            raw_path = chunk.get("file_path", "unknown")
            filename = raw_path.split("/")[-1].split("\\")[-1] if raw_path != "unknown" else "unknown"
            context_parts.append(
                f"**Chunk {i}** (score: {chunk.get('score', 'N/A'):.3f}, "
                f"entity: `{chunk.get('qualified_name', 'unknown')}`, "
                f"type: {chunk.get('entity_type', 'unknown')}, "
                f"file: `{filename}`):\n"
                f"```\n{text_preview}\n```\n"
            )

    if not context_parts:
        context_parts.append("No data was retrieved from either source.")

    context = "\n".join(context_parts)

    # Use the latest user turn (matches query_analyzer_node)
    user_query = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            user_query = msg.content
            break

    response = invoke_llm([
        SystemMessage(content=SYNTHESIZER_SYSTEM_PROMPT),
        HumanMessage(
            content=f"## User Question\n{user_query}\n\n## Retrieved Context\n{context}"
        ),
    ])

    answer = response.content.strip()
    current_attempts = state.get("retrieval_attempts", 0) + 1

    logger.info(
        f"[Synthesizer] Generated answer ({len(answer)} chars), "
        f"attempt {current_attempts}/{settings.max_retrieval_retries + 1}"
    )

    return {
        "final_answer": answer,
        "retrieval_attempts": current_attempts,
        "messages": [AIMessage(content=f"[Synthesizer] Answer generated (attempt {current_attempts}).")],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. CONDITIONAL EDGE FUNCTIONS (Routing Logic)
# ══════════════════════════════════════════════════════════════════════════════


def route_after_analysis(state: GraphRAGState) -> str:
    """
    Conditional edge: Route based on the query analyzer's strategy decision.

    Returns the name of the next node to execute:
      - "cypher_generation" for graph-only queries
      - "vector_search" for semantic-only queries
      - "cypher_generation" for hybrid (cypher first, then vector)
    """
    strategy = state.get("query_plan", {}).get("strategy", "hybrid")

    if strategy == "graph":
        return "cypher_generation"
    elif strategy == "vector":
        return "vector_search"
    else:  # "hybrid" or unknown → default to full pipeline
        return "cypher_generation"


def route_after_cypher(state: GraphRAGState) -> str:
    """
    Conditional edge: After Cypher execution, decide whether to also
    run vector search (hybrid) or go straight to synthesis.
    """
    strategy = state.get("query_plan", {}).get("strategy", "hybrid")

    if strategy == "hybrid":
        return "vector_search"
    else:
        return "synthesizer"


def route_after_synthesis(state: GraphRAGState) -> str:
    """
    Conditional edge: Self-correction loop.

    If the synthesizer's answer starts with "[INSUFFICIENT_DATA]" and we
    haven't exceeded the max retry count, loop back to retrieval.
    Otherwise, proceed to END.
    """
    answer = state.get("final_answer", "")
    attempts = state.get("retrieval_attempts", 0)
    max_retries = settings.max_retrieval_retries

    if answer.startswith("[INSUFFICIENT_DATA]") and attempts <= max_retries:
        logger.info(
            f"[Router] Insufficient data detected (attempt {attempts}/{max_retries}). "
            "Re-routing to retrieval."
        )
        # Route back based on strategy
        strategy = state.get("query_plan", {}).get("strategy", "hybrid")
        if strategy == "graph":
            return "cypher_generation"
        elif strategy == "vector":
            return "vector_search"
        else:
            return "cypher_generation"
    else:
        return END


# ══════════════════════════════════════════════════════════════════════════════
# 5. GRAPH CONSTRUCTION & COMPILATION
# ══════════════════════════════════════════════════════════════════════════════


def build_graph() -> StateGraph:
    """
    Construct the LangGraph StateGraph with all nodes and edges.

    Graph topology:
        START → query_analyzer → (conditional) → cypher_generation / vector_search
        cypher_generation → (conditional) → vector_search / synthesizer
        vector_search → synthesizer
        synthesizer → (conditional) → END / retry loop
    """
    # Initialize the graph with our state schema
    graph = StateGraph(GraphRAGState)

    # ── Register Nodes ───────────────────────────────────────────────────
    graph.add_node("query_analyzer", query_analyzer_node)
    graph.add_node("cypher_generation", cypher_generation_node)
    graph.add_node("vector_search", vector_search_node)
    graph.add_node("synthesizer", synthesizer_node)

    # ── Entry Point ──────────────────────────────────────────────────────
    graph.set_entry_point("query_analyzer")

    # ── Edges ────────────────────────────────────────────────────────────

    # After query analysis, route to the appropriate retrieval node(s)
    graph.add_conditional_edges(
        "query_analyzer",
        route_after_analysis,
        {
            "cypher_generation": "cypher_generation",
            "vector_search": "vector_search",
        },
    )

    # After Cypher generation, optionally chain to vector search (hybrid)
    graph.add_conditional_edges(
        "cypher_generation",
        route_after_cypher,
        {
            "vector_search": "vector_search",
            "synthesizer": "synthesizer",
        },
    )

    # Vector search always flows to the synthesizer
    graph.add_edge("vector_search", "synthesizer")

    # After synthesis, either END or loop back for self-correction
    graph.add_conditional_edges(
        "synthesizer",
        route_after_synthesis,
        {
            "cypher_generation": "cypher_generation",
            "vector_search": "vector_search",
            END: END,
        },
    )

    return graph


def compile_graph(checkpointer=None):
    """
    Build and compile the graph into a runnable LangGraph application.

    Args:
        checkpointer: Optional LangGraph checkpointer (e.g. SqliteSaver).
                      When provided, every invocation persists its state so
                      conversations can be resumed via thread_id.

    The compiled graph is a Pregel-based execution engine that:
      - Manages state transitions automatically
      - Supports checkpointing for durability
      - Provides streaming and async invocation
    """
    graph = build_graph()
    compiled = graph.compile(checkpointer=checkpointer)
    logger.info(
        "LangGraph compiled successfully%s.",
        " (with checkpointer)" if checkpointer is not None else "",
    )
    return compiled


def get_sqlite_checkpointer(db_path: str = "checkpoints.db"):
    """
    Create and return a SqliteSaver checkpointer backed by *db_path*.

    Returns a long-lived saver instance (not the ``from_conn_string`` context
    manager). The caller must close ``checkpointer.conn`` on shutdown.

    Raises:
        ImportError: If neither langgraph.checkpoint.sqlite nor
                     langgraph_checkpoint_sqlite are installed.
    """
    import sqlite3

    if SqliteSaver is None:
        raise ImportError(
            "SqliteSaver not available. Install langgraph-checkpoint-sqlite: "
            "pip install langgraph-checkpoint-sqlite"
        )
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)


# ══════════════════════════════════════════════════════════════════════════════
# 6. CONVENIENCE: Direct Invocation
# ══════════════════════════════════════════════════════════════════════════════


# Module-level compiled graph (lazy singleton, no checkpointer — for CLI use)
_compiled_graph = None


def get_compiled_graph():
    """Get or create the compiled graph singleton (no checkpointer)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = compile_graph()
    return _compiled_graph


def invoke(query: str) -> dict:
    """
    High-level convenience function to run a query through the full pipeline.

    Args:
        query: Natural language question about the codebase.

    Returns:
        The final state dict containing the answer and all intermediate data.
    """
    app = get_compiled_graph()

    initial_state: GraphRAGState = {
        "messages": [HumanMessage(content=query)],
        "query_plan": {},
        "retrieved_graph_data": [],
        "retrieved_vector_data": [],
        "final_answer": "",
        "retrieval_attempts": 0,
    }

    # Run the graph to completion
    final_state = app.invoke(initial_state)
    return final_state


# ══════════════════════════════════════════════════════════════════════════════
# CLI — Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    test_query = sys.argv[1] if len(sys.argv) > 1 else "What are the main modules in this codebase?"
    print(f"\n{'='*60}")
    print(f"Query: {test_query}")
    print(f"{'='*60}\n")

    result = invoke(test_query)

    print(f"\n{'='*60}")
    print("FINAL ANSWER:")
    print(f"{'='*60}")
    print(result.get("final_answer", "No answer generated."))
