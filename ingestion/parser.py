from __future__ import annotations

"""
==============================================================================
Codebase Ingestion Pipeline — parser.py
==============================================================================
This module is the **data engineering backbone** of the CodeGraph AI
Codebase Analyzer. It performs three stages:

  1. **AST Parsing** — Walks a target directory, parses every `.py` file with
     Python's `ast` module, and extracts structural entities (modules, classes,
     functions) plus their relationships (IMPORTS, DEFINES, CALLS).

  2. **Knowledge Graph Ingestion (Neo4j)** — Writes the extracted entities as
     nodes and relationships into Neo4j using MERGE-based idempotent Cypher
     queries (safe for re-runs).

  3. **Vector Database Ingestion (Qdrant)** — Chunks docstrings and raw source
     code, embeds them via the configured embedding model, and upserts the
     vectors + metadata into Qdrant for semantic retrieval.

Design Decisions:
  - All Neo4j writes use MERGE (not CREATE) to make the pipeline idempotent.
  - Embedding calls are batched to respect rate limits and minimize latency.
  - The parser is decoupled from the agent layer — it's a CLI-invocable
    script (python -m ingestion.parser) AND importable as a library.

Usage:
    python -m ingestion.parser                     # uses TARGET_CODEBASE_PATH from .env
    python -m ingestion.parser /path/to/codebase   # explicit path override
==============================================================================
"""

import ast
import hashlib
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

from neo4j import GraphDatabase
from qdrant_client import QdrantClient
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("ingestion.parser")


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 — AST Extraction
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ExtractedEntity:
    """
    A single structural entity discovered by the AST walker.

    Fields:
        entity_type : One of "module", "class", "function".
        qualified_name : Dot-separated fully-qualified name (e.g., "pkg.mod.ClassName.method").
        file_path : Absolute path to the source file.
        start_line : Starting line number in the source file.
        end_line : Ending line number in the source file.
        docstring : The entity's docstring (empty string if absent).
        source_code : Raw source text of the entity.
        parent_qname : Qualified name of the containing entity (None for top-level modules).
    """
    entity_type: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    docstring: str
    source_code: str
    parent_qname: Optional[str] = None


@dataclass
class ExtractedRelationship:
    """
    A directed edge between two entities.

    Fields:
        source_qname : Qualified name of the source entity.
        target_qname : Qualified name of the target entity.
        rel_type : One of "IMPORTS", "DEFINES", "CALLS", "CONTAINS".
    """
    source_qname: str
    target_qname: str
    rel_type: str


@dataclass
class ParseResult:
    """Aggregated output from a full codebase parse."""
    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)


class CodebaseASTVisitor(ast.NodeVisitor):
    """
    AST visitor that recursively extracts structural entities and relationships
    from a single Python source file.

    Walk strategy:
      - Module-level: capture imports, top-level functions, top-level classes.
      - Class-level: capture methods and nested classes.
      - Function-level: capture function calls (simple name resolution).

    Limitations (documented for transparency):
      - Call resolution is name-based, not type-aware. `foo.bar()` resolves
        to the attribute chain string, but we don't perform type inference.
      - Star imports (`from x import *`) are recorded as importing module `x`.
    """

    def __init__(self, file_path: str, module_qname: str, source_lines: list[str]):
        self.file_path = file_path
        self.module_qname = module_qname
        self.source_lines = source_lines
        self.result = ParseResult()

        # Register the module itself as an entity
        full_source = "\n".join(source_lines)
        module_doc = ast.get_docstring(ast.parse(full_source)) or ""
        self.result.entities.append(
            ExtractedEntity(
                entity_type="module",
                qualified_name=module_qname,
                file_path=file_path,
                start_line=1,
                end_line=len(source_lines),
                docstring=module_doc,
                source_code=full_source[:2000],  # Cap module source for embedding
            )
        )

        # Stack tracks the current scope for qualified-name construction
        self._scope_stack: list[str] = [module_qname]

    # ── Helpers ──────────────────────────────────────────────────────────

    def _current_scope(self) -> str:
        return self._scope_stack[-1]

    def _make_qname(self, name: str) -> str:
        return f"{self._current_scope()}.{name}"

    def _extract_source(self, node: ast.AST) -> str:
        """Extract raw source lines for a given AST node."""
        try:
            return ast.get_source_segment("\n".join(self.source_lines), node) or ""
        except Exception:
            # Fallback: use line range
            start = getattr(node, "lineno", 1) - 1
            end = getattr(node, "end_lineno", start + 1)
            return "\n".join(self.source_lines[start:end])

    # ── Visitors ─────────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        """Handle `import foo, bar` statements."""
        for alias in node.names:
            self.result.relationships.append(
                ExtractedRelationship(
                    source_qname=self._current_scope(),
                    target_qname=alias.name,
                    rel_type="IMPORTS",
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Handle `from foo import bar` statements."""
        module = node.module or ""
        for alias in node.names:
            target = f"{module}.{alias.name}" if module else alias.name
            self.result.relationships.append(
                ExtractedRelationship(
                    source_qname=self._current_scope(),
                    target_qname=target,
                    rel_type="IMPORTS",
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Extract class entity and DEFINES/CONTAINS relationships."""
        qname = self._make_qname(node.name)
        docstring = ast.get_docstring(node) or ""
        source = self._extract_source(node)

        self.result.entities.append(
            ExtractedEntity(
                entity_type="class",
                qualified_name=qname,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                docstring=docstring,
                source_code=source[:3000],  # Cap for embedding
                parent_qname=self._current_scope(),
            )
        )

        # The current scope DEFINES this class
        self.result.relationships.append(
            ExtractedRelationship(
                source_qname=self._current_scope(),
                target_qname=qname,
                rel_type="DEFINES",
            )
        )

        # Recurse into the class body with updated scope
        self._scope_stack.append(qname)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Extract function/method entity and DEFINES relationship."""
        qname = self._make_qname(node.name)
        docstring = ast.get_docstring(node) or ""
        source = self._extract_source(node)

        self.result.entities.append(
            ExtractedEntity(
                entity_type="function",
                qualified_name=qname,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                docstring=docstring,
                source_code=source[:3000],
                parent_qname=self._current_scope(),
            )
        )

        self.result.relationships.append(
            ExtractedRelationship(
                source_qname=self._current_scope(),
                target_qname=qname,
                rel_type="DEFINES",
            )
        )

        # Recurse into the function body with updated scope
        self._scope_stack.append(qname)
        self.generic_visit(node)
        self._scope_stack.pop()

    # Async functions follow the same extraction logic
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        """
        Extract CALLS relationships from function call expressions.

        Handles:
          - Simple calls: `foo()`       → CALLS "foo"
          - Attribute calls: `obj.foo()` → CALLS "obj.foo"
          - Chained calls: `a.b.c()`    → CALLS "a.b.c"
        """
        callee_name = self._resolve_call_name(node.func)
        if callee_name:
            self.result.relationships.append(
                ExtractedRelationship(
                    source_qname=self._current_scope(),
                    target_qname=callee_name,
                    rel_type="CALLS",
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _resolve_call_name(node: ast.expr) -> Optional[str]:
        """Recursively resolve a call target to a dotted name string."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = CodebaseASTVisitor._resolve_call_name(node.value)
            if parent:
                return f"{parent}.{node.attr}"
            return node.attr
        return None


def parse_codebase(root_dir: str) -> ParseResult:
    """
    Walk a directory tree, parse all `.py` files, and return aggregated
    structural entities and relationships.

    Args:
        root_dir: Absolute or relative path to the codebase root.

    Returns:
        ParseResult containing all discovered entities and relationships.
    """
    root = Path(root_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Codebase root not found: {root}")

    aggregated = ParseResult()
    py_files = sorted(root.rglob("*.py"))
    logger.info(f"Discovered {len(py_files)} Python files in {root}")

    for py_file in py_files:
        # Skip hidden directories and common non-source dirs
        parts = py_file.relative_to(root).parts
        if any(p.startswith(".") or p in {"__pycache__", "venv", ".venv", "node_modules"} for p in parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as e:
            logger.warning(f"Syntax error in {py_file}: {e}. Skipping.")
            continue

        # Build a dotted module name from the relative path
        relative = py_file.relative_to(root)
        module_parts = list(relative.parts)
        if module_parts[-1] == "__init__.py":
            module_parts = module_parts[:-1]  # package itself
        else:
            module_parts[-1] = module_parts[-1].removesuffix(".py")
        module_qname = ".".join(module_parts) if module_parts else relative.stem

        source_lines = source.splitlines()
        visitor = CodebaseASTVisitor(
            file_path=str(py_file),
            module_qname=module_qname,
            source_lines=source_lines,
        )
        visitor.visit(tree)

        aggregated.entities.extend(visitor.result.entities)
        aggregated.relationships.extend(visitor.result.relationships)

    logger.info(
        f"Extraction complete: {len(aggregated.entities)} entities, "
        f"{len(aggregated.relationships)} relationships"
    )
    return aggregated


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
                 end_line, docstring_preview (first 200 chars).
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
                        n.entity_type = $entity_type
                """)
                session.run(
                    cypher,
                    qname=entity.qualified_name,
                    file_path=entity.file_path,
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                    docstring_preview=entity.docstring[:200],
                    entity_type=entity.entity_type,
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
        host: str,
        port: int,
        collection_name: str,
        embedding_model: str,
        embedding_dim: int,
        api_key: str | None = None,
        api_base: str | None = None,
    ):
        self.client = QdrantClient(host=host, port=port)
        self.collection_name = collection_name
        self.embedding_dim = embedding_dim

        # Build embedding function for Gemini
        self.embeddings = GoogleGenerativeAIEmbeddings(
            model=embedding_model,
            google_api_key=api_key,
            output_dimensionality=embedding_dim,
        )

        self._ensure_collection()
        logger.info(f"Qdrant writer initialized → {host}:{port}/{collection_name}")

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


def run_ingestion(codebase_path: Optional[str] = None) -> dict[str, int]:
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
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        collection_name=settings.qdrant_collection_name,
        embedding_model=settings.google_embedding_model,
        embedding_dim=settings.embedding_dimension,
        api_key=settings.google_api_key,
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
