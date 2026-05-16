"""
Shared Qdrant client factory — local Docker or Qdrant Cloud (URL + API key).
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from config import settings


def get_qdrant_client() -> QdrantClient:
    """
    Build a Qdrant client from settings.

    Priority:
      1. QDRANT_URL (+ optional QDRANT_API_KEY) — Qdrant Cloud / custom HTTPS endpoint
      2. QDRANT_HOST + QDRANT_PORT (+ optional QDRANT_API_KEY) — local docker-compose
    """
    api_key = (settings.qdrant_api_key or "").strip() or None
    url = (settings.qdrant_url or "").strip()

    if url:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return QdrantClient(url=url, api_key=api_key, timeout=60)

    kwargs: dict = {
        "host": settings.qdrant_host,
        "port": settings.qdrant_port,
    }
    if api_key:
        kwargs["api_key"] = api_key
    return QdrantClient(**kwargs)


def qdrant_connection_label() -> str:
    """Human-readable target for logs."""
    if settings.qdrant_url:
        return settings.qdrant_url.strip()
    return f"{settings.qdrant_host}:{settings.qdrant_port}"
