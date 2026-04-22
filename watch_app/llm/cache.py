"""Content-hash-keyed cache for LLM responses.

Every LLM call is keyed by SHA256(model + prompt_version + input). If we've
seen this exact request before, we return the cached response and skip the
API call. Critical for cost control.

Cache is stored in the main SQLite DB in the `llm_cache` table (see
storage.models.LLMCache) — same DB as everything else, no separate file.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from watch_app.storage.models import LLMCache


def compute_cache_key(model: str, prompt_version: str, input_content: str) -> str:
    """Return the SHA256 hex digest used as the cache key."""
    payload = f"{model}\x00{prompt_version}\x00{input_content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached(
    session: Session,
    model: str,
    prompt_version: str,
    input_content: str,
) -> dict[str, Any] | None:
    """Return the cached response dict, or None if no cache entry exists."""
    key = compute_cache_key(model, prompt_version, input_content)
    row = session.get(LLMCache, key)
    return row.response if row else None


def set_cached(
    session: Session,
    model: str,
    prompt_version: str,
    input_content: str,
    response: dict[str, Any],
) -> None:
    """Write a response to the cache. Safe to call even if an entry exists
    (upserts)."""
    key = compute_cache_key(model, prompt_version, input_content)
    existing = session.get(LLMCache, key)
    if existing:
        existing.response = response
    else:
        session.add(
            LLMCache(
                cache_key=key,
                model=model,
                prompt_version=prompt_version,
                response=response,
            )
        )


def _normalize_for_hashing(payload: dict[str, Any]) -> str:
    """Deterministic JSON encoding for anything being fed into the cache key."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)
