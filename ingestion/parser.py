from __future__ import annotations

"""
==============================================================================
Codebase Ingestion Pipeline — parser.py
==============================================================================
This module is the **data engineering backbone** of the CodeGraph AI
Codebase Analyzer. It performs three stages:

  1. **Multi-language Parsing (Tree-sitter)** — Walks a target directory, parses
     every supported source file (Python, JavaScript/TypeScript, Java, Go, Rust,
     C/C++, C#, Ruby, PHP, ...) with Tree-sitter, and extracts structural
     entities (modules, classes, functions) plus their relationships
     (IMPORTS, DEFINES, CALLS). See `ingestion/treesitter_parser.py`.

  2. **Knowledge Graph Ingestion (Neo4j)** — Writes the extracted entities as
     nodes and relationships into Neo4j using MERGE-based idempotent Cypher
     queries (safe for re-runs).

  3. **Vector Database Ingestion (Qdrant)** — Chunks docstrings and raw source
     code, embeds them via the configured embedding model, and upserts the
     vectors + metadata into Qdrant for semantic retrieval.

Design Decisions:
  - Parsing is delegated to a language-agnostic Tree-sitter walker so the same
    pipeline works across many programming languages.
  - All Neo4j writes use MERGE (not CREATE) to make the pipeline idempotent.
  - Embedding calls are batched to respect rate limits and minimize latency.
  - The parser is decoupled from the agent layer — it's a CLI-invocable
    script (python -m ingestion.parser) AND importable as a library.

Usage:
    python -m ingestion.parser                     # uses TARGET_CODEBASE_PATH from .env
    python -m ingestion.parser /path/to/codebase   # explicit path override
==============================================================================
"""

import hashlib
import logging
import sys
import uuid
from pathlib import Path
from textwrap import dedent

from neo4j import GraphDatabase
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)
from langchain_google_genai import GoogleGenerativeAIEmbeddings

# ── Local imports ────────────────────────────────────────────────────────────
# Adjust sys.path so this module can be run both as `python -m ingestion.parser`
# and imported from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402
from qdrant_util import get_qdrant_client, qdrant_connection_label  # noqa: E402

# Tree-sitter based, multi-language extraction (Stage 1). The data model
# (ExtractedEntity / ExtractedRelationship / ParseResult) and `parse_codebase`
# keep the same public shape the rest of the pipeline relies on.
from ingestion.treesitter_parser import (  # noqa: E402
    ExtractedEntity,
    ExtractedRelationship,
    ParseResult,
    parse_codebase,
    parse_file,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("ingestion.parser")

# Re-export so existing imports like `from ingestion.parser import parse_codebase`
# keep working unchanged.
__all__ = [
    "ExtractedEntity",
    "ExtractedRelationship",
    "ParseResult",
    "parse_codebase",
    "parse_file",
    "Neo4jWriter",
    "QdrantWriter",
    "run_ingestion",
]


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — Neo4j Knowledge Graph Ingestion
# ══════════════════════════════════════════════════════════════════════════════


class Neo4jWriter:
    """
    Writes extracted entities and relationships into Neo4j using idempotent
    MERGE operations. Designed for safe re-runs — re-ingesting the same
    codebase will update existing nodes rather than duplicate them.

    Schema:
        Nodes:   (:Module), (:Class), (:Function)
                 All share properties: qualified_name, file_path, start_line,
                 end_line, docstring_preview (first 200 chars), entity_type,
                 plus language and kind (fine-grained type) for multi-language
                 context.
        Edges:   [:IMPORTS], [:DEFINES], [:CALLS], [:CONTAINS]
    """

    # Mapping from entity_type string to Neo4j label
    LABEL_MAP = {
        "module": "Module",
        "class": "Class",
        "function": "Function",
    }

    def __init__(self, uri: str, username: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        logger.info(f"Neo4j driver initialized → {uri}")

    def close(self) -> None:
        self.driver.close()

    def ensure_constraints(self) -> None:
        """
        Create uniqueness constraints on qualified_name for each label.
        This also implicitly creates indexes for fast lookups.
        """
        with self.driver.session() as session:
            for label in self.LABEL_MAP.values():
                cypher = (
                    f"CREATE CONSTRAINT IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.qualified_name IS UNIQUE"
                )
                session.run(cypher)
                logger.info(f"Ensured uniqueness constraint on :{label}(qualified_name)")

    def write_entities(self, entities: list[ExtractedEntity]) -> None:
        """Batch-MERGE all entities into Neo4j."""
        with self.driver.session() as session:
            for entity in entities:
                label = self.LABEL_MAP.get(entity.entity_type, "CodeEntity")
                cypher = dedent(f"""\
                    MERGE (n:{label} {{qualified_name: $qname}})
                    SET n.file_path   = $file_path,
                        n.start_line  = $start_line,
                        n.end_line    = $end_line,
                        n.docstring_preview = $docstring_preview,
                        n.entity_type = $entity_type,
                        n.language    = $language,
                        n.kind        = $kind
                """)
                session.run(
                    cypher,
                    qname=entity.qualified_name,
                    file_path=entity.file_path,
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                    docstring_preview=entity.docstring[:200],
                    entity_type=entity.entity_type,
                    language=getattr(entity, "language", "") or "",
                    kind=getattr(entity, "kind", "") or entity.entity_type,
                )
        logger.info(f"Wrote {len(entities)} entity nodes to Neo4j")

    def write_relationships(self, relationships: list[ExtractedRelationship]) -> None:
        """
        Batch-MERGE all relationships.

        Because target entities may be external (stdlib, third-party), we
        MERGE a generic CodeEntity node for any unresolved target. This
        preserves the call graph even for external dependencies.
        """
        with self.driver.session() as session:
            for rel in relationships:
                # Use a generic merge — source and target might be any label.
                # The ON CREATE clause tags externals so we can filter later.
                cypher = dedent(f"""\
                    MERGE (src {{qualified_name: $src_qname}})
                    MERGE (tgt {{qualified_name: $tgt_qname}})
                    ON CREATE SET tgt.external = true
                    MERGE (src)-[r:{rel.rel_type}]->(tgt)
                """)
                session.run(
                    cypher,
                    src_qname=rel.source_qname,
                    tgt_qname=rel.target_qname,
                )
        logger.info(f"Wrote {len(relationships)} relationships to Neo4j")


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3 — Qdrant Vector Database Ingestion
# ══════════════════════════════════════════════════════════════════════════════


class QdrantWriter:
    """
    Embeds and upserts code chunks into Qdrant for semantic retrieval.

    Each entity becomes one or more Qdrant points:
      - One point for the docstring (if present).
      - One point for the raw source code.

    Payload metadata includes: qualified_name, entity_type, file_path,
    start_line, end_line — enabling filtered search (e.g., "only functions").
    """

    def __init__(
        self,
        collection_name: str,
        embedding_model: str,
        embedding_dim: int,
        google_api_key: str | None = None,
    ):
        self.client = get_qdrant_client()
        self.collection_name = collection_name
        self.embedding_dim = embedding_dim

        # Build embedding function for Gemini
        self.embeddings = GoogleGenerativeAIEmbeddings(
            model=embedding_model,
            google_api_key=google_api_key,
            output_dimensionality=embedding_dim,
        )

        self._ensure_collection()
        logger.info(
            f"Qdrant writer initialized → {qdrant_connection_label()}/{collection_name}"
        )

    def _ensure_collection(self) -> None:
        """Create the collection if it doesn't already exist."""
        collections = [c.name for c in self.client.get_collections().collections]
        if self.collection_name not in collections:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection: {self.collection_name}")
        else:
            logger.info(f"Qdrant collection already exists: {self.collection_name}")

    def _deterministic_id(self, text: str) -> str:
        """Generate a deterministic UUID from text for idempotent upserts."""
        return str(uuid.UUID(hashlib.md5(text.encode()).hexdigest()))

    def ingest_entities(
        self, entities: list[ExtractedEntity], batch_size: int = 64
    ) -> None:
        """
        Embed and upsert entities into Qdrant in batches.

        For each entity, we create up to 2 points:
          1. Docstring chunk (if docstring is non-empty).
          2. Source code chunk.

        Using deterministic IDs ensures re-ingestion overwrites rather than
        duplicates.
        """
        points_buffer: list[PointStruct] = []
        texts_buffer: list[str] = []  # Parallel list for batch embedding

        for entity in entities:
            chunks: list[tuple[str, str]] = []  # (chunk_type, text)

            if entity.docstring.strip():
                chunks.append(("docstring", entity.docstring.strip()))

            if entity.source_code.strip():
                chunks.append(("source", entity.source_code.strip()))

            for chunk_type, text in chunks:
                # Create a composite ID from qualified name + chunk type
                point_id = self._deterministic_id(
                    f"{entity.qualified_name}::{chunk_type}"
                )
                payload = {
                    "qualified_name": entity.qualified_name,
                    "entity_type": entity.entity_type,
                    "chunk_type": chunk_type,
                    "file_path": entity.file_path,
                    "start_line": entity.start_line,
                    "end_line": entity.end_line,
                    "language": getattr(entity, "language", "") or "",
                    "kind": getattr(entity, "kind", "") or entity.entity_type,
                    "text": text[:2000],  # Store truncated text in payload
                }
                # Placeholder — vector will be filled during batch embed
                points_buffer.append(
                    PointStruct(id=point_id, vector=[], payload=payload)
                )
                texts_buffer.append(text[:2000])

        # ── Batch embedding and upsert ───────────────────────────────────
        logger.info(f"Embedding {len(texts_buffer)} chunks in batches of {batch_size}...")
        for i in range(0, len(texts_buffer), batch_size):
            batch_texts = texts_buffer[i : i + batch_size]
            batch_points = points_buffer[i : i + batch_size]

            try:
                vectors = self.embeddings.embed_documents(batch_texts)
            except Exception as e:
                logger.error(f"Embedding batch {i // batch_size} failed: {e}")
                continue

            # Assign computed vectors to the point structs
            for point, vector in zip(batch_points, vectors):
                point.vector = vector

            self.client.upsert(
                collection_name=self.collection_name,
                points=batch_points,
            )
            logger.info(
                f"  Upserted batch {i // batch_size + 1} "
                f"({len(batch_points)} points)"
            )

        logger.info(f"Qdrant ingestion complete: {len(points_buffer)} total points")


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator — Ties the three stages together
# ══════════════════════════════════════════════════════════════════════════════


def run_ingestion(codebase_path: str | None = None) -> dict[str, int]:
    """
    End-to-end ingestion pipeline: Parse → Neo4j → Qdrant.

    Args:
        codebase_path: Path to the codebase root. Falls back to settings.

    Returns:
        Summary dict with counts of ingested entities and relationships.
    """
    target = codebase_path or settings.target_codebase_path
    logger.info(f"{'='*60}")
    logger.info(f"Starting ingestion pipeline for: {target}")
    logger.info(f"{'='*60}")

    # ── Stage 1: Parse ───────────────────────────────────────────────────
    result = parse_codebase(target)

    # ── Stage 2: Neo4j ───────────────────────────────────────────────────
    neo4j_writer = Neo4jWriter(
        uri=settings.neo4j_uri,
        username=settings.neo4j_username,
        password=settings.neo4j_password,
    )
    try:
        neo4j_writer.ensure_constraints()
        neo4j_writer.write_entities(result.entities)
        neo4j_writer.write_relationships(result.relationships)
    finally:
        neo4j_writer.close()

    # ── Stage 3: Qdrant ──────────────────────────────────────────────────
    qdrant_writer = QdrantWriter(
        collection_name=settings.qdrant_collection_name,
        embedding_model=settings.google_embedding_model,
        embedding_dim=settings.embedding_dimension,
        google_api_key=settings.google_api_key,
    )
    qdrant_writer.ingest_entities(result.entities)

    summary = {
        "entities_parsed": len(result.entities),
        "relationships_parsed": len(result.relationships),
        "target_path": target,
    }
    logger.info(f"Ingestion complete: {summary}")
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Accept an optional CLI argument for the codebase path
    path_override = sys.argv[1] if len(sys.argv) > 1 else None
    run_ingestion(path_override)
