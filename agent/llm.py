"""
LLM invocation with optional Groq fallback when Google Gemini quota is exceeded.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage

from config import settings

logger = logging.getLogger("agent.llm")


def _has_google() -> bool:
    key = (settings.google_api_key or "").strip()
    return bool(key) and key.lower() not in ("placeholder", "your-google-api-key")


def _has_groq() -> bool:
    key = (settings.groq_api_key or "").strip()
    return bool(key) and key.lower() not in ("placeholder", "your-groq-api-key")


def is_quota_or_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "resource_exhausted",
            "429",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
        )
    )


def _normalize_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(content).strip()


def _build_google_llm():
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.google_model_name,
        google_api_key=settings.google_api_key,
        temperature=0.0,
        max_retries=1,
    )


def _build_groq_llm():
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.groq_model_name,
        api_key=settings.groq_api_key,
        temperature=0.0,
        max_retries=1,
    )


def _provider_chain() -> list[tuple[str, Callable[[list[BaseMessage]], AIMessage]]]:
    """Ordered providers: primary first, then fallback when enabled."""

    def google_invoke(messages: list[BaseMessage]) -> AIMessage:
        return _build_google_llm().invoke(messages)

    def groq_invoke(messages: list[BaseMessage]) -> AIMessage:
        return _build_groq_llm().invoke(messages)

    google = ("google", google_invoke) if _has_google() else None
    groq = ("groq", groq_invoke) if _has_groq() else None

    chain: list[tuple[str, Callable[[list[BaseMessage]], AIMessage]]] = []
    primary = (settings.llm_primary or "google").strip().lower()

    if primary == "groq":
        if groq:
            chain.append(groq)
        if google and settings.llm_fallback_enabled:
            chain.append(google)
    else:
        if google:
            chain.append(google)
        if groq and settings.llm_fallback_enabled:
            chain.append(groq)

    return chain


def invoke_llm(messages: list[BaseMessage]) -> AIMessage:
    """
    Invoke the configured chat model. On quota/rate-limit errors, tries the
    next provider in the chain (e.g. Gemini → Groq).
    """
    chain = _provider_chain()
    if not chain:
        raise RuntimeError(
            "No LLM provider configured. Set GOOGLE_API_KEY and/or GROQ_API_KEY in .env"
        )

    last_exc: Exception | None = None
    for i, (name, invoke_fn) in enumerate(chain):
        try:
            response = invoke_fn(messages)
            content = _normalize_content(response.content)
            if i > 0:
                logger.info("LLM response from fallback provider: %s", name)
            return AIMessage(content=content)
        except Exception as exc:
            last_exc = exc
            has_next = i < len(chain) - 1
            if has_next and is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "LLM provider %s hit quota/rate limit, trying fallback: %s",
                    name,
                    exc,
                )
                continue
            raise

    assert last_exc is not None
    raise last_exc
