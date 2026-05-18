from __future__ import annotations

"""
==============================================================================
Centralized Configuration — Pydantic Settings
==============================================================================
Single source of truth for all service connection parameters, model names,
and tunable knobs. Reads from environment variables / .env file automatically.

Usage:
    from config import settings
    print(settings.neo4j_uri)
==============================================================================
"""

import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Typed, validated application settings.
    All values can be overridden via environment variables or a `.env` file.
    """

    # ── LLM Provider (Google Gemini) ──────────────────────────────────────
    google_api_key: str = Field(
        default="placeholder",
        description="API key for Google Gemini (AI Studio).",
    )
    google_model_name: str = Field(
        default="gemini-2.5-flash",
        description="Chat model identifier (e.g., gemini-2.5-flash, gemini-2.5-pro).",
    )
    google_embedding_model: str = Field(
        default="gemini-embedding-001",
        description="Embedding model identifier.",
    )
    embedding_dimension: int = Field(
        default=768,
        description="Embedding vector size (768 when using gemini-embedding-001 with reduced dims).",
    )

    # ── LLM fallback (Groq) ───────────────────────────────────────────────
    groq_api_key: Optional[str] = Field(
        default=None,
        description="Groq API key for chat fallback (https://console.groq.com/).",
    )
    groq_model_name: str = Field(
        default="llama-3.3-70b-versatile",
        description="Groq chat model when used as primary or fallback.",
    )
    llm_primary: str = Field(
        default="google",
        description='Primary chat provider: "google" or "groq".',
    )
    llm_fallback_enabled: bool = Field(
        default=True,
        description="If true, switch to the other provider on quota/rate-limit errors.",
    )

    # ── Neo4j ────────────────────────────────────────────────────────────
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        description="Bolt URI for the Neo4j instance.",
    )
    neo4j_username: str = Field(default="neo4j")
    neo4j_password: str = Field(default="graphrag2024")

    # ── Qdrant ───────────────────────────────────────────────────────────
    qdrant_url: Optional[str] = Field(
        default=None,
        description=(
            "Full Qdrant URL for cloud/managed instances, e.g. "
            "https://xxxx.cloud.qdrant.io:6333. When set, overrides host/port."
        ),
    )
    qdrant_api_key: Optional[str] = Field(
        default=None,
        description="API key for Qdrant Cloud (optional for local Docker).",
    )
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)
    qdrant_collection_name: str = Field(default="codebase_chunks")

    # ── Ingestion ────────────────────────────────────────────────────────
    target_codebase_path: str = Field(
        default="./sample_codebase",
        description="Root directory of the Python codebase to analyze.",
    )
    github_clone_root: str = Field(
        default=".cache/github_repos",
        description="Directory where GitHub repos are shallow-cloned before ingestion.",
    )

    # ── Chunking ─────────────────────────────────────────────────────────
    chunk_max_tokens: int = Field(
        default=512,
        description="Maximum token count per text chunk sent to the vector DB.",
    )

    # ── Agent Tuning ─────────────────────────────────────────────────────
    max_retrieval_retries: int = Field(
        default=2,
        description="Max self-correction loops before the synthesizer gives up.",
    )

    model_config = SettingsConfigDict(
        # Load .env only for local dev; Cloud Run must use service env vars.
        env_file=".env" if Path(".env").is_file() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# ── Singleton ────────────────────────────────────────────────────────────────
settings = Settings()


def is_cloud_run() -> bool:
    """True when running on Google Cloud Run (K_SERVICE is injected)."""
    return bool(os.getenv("K_SERVICE"))


def is_local_neo4j_uri(uri: str | None = None) -> bool:
    u = (uri or settings.neo4j_uri).lower()
    return "localhost" in u or "127.0.0.1" in u


def validate_production_settings() -> list[str]:
    """Return human-readable config errors (empty if OK)."""
    errors: list[str] = []
    if not is_cloud_run():
        return errors

    if is_local_neo4j_uri():
        errors.append(
            "NEO4J_URI is localhost. On Cloud Run set NEO4J_URI to your Neo4j Aura URL "
            "(e.g. neo4j+s://xxxx.databases.neo4j.io) via --env-vars-file or the console."
        )
    if settings.qdrant_url is None and settings.qdrant_host in ("localhost", "127.0.0.1"):
        errors.append(
            "Qdrant points at localhost. Set QDRANT_URL and QDRANT_API_KEY for Qdrant Cloud."
        )
    if settings.google_api_key in ("", "placeholder"):
        errors.append("GOOGLE_API_KEY is missing on Cloud Run.")
    return errors
