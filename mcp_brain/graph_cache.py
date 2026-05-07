"""TTL-based in-memory cache for the ``knowledge_graph`` tool payload.

The payload is expensive to compute on a vault with thousands of files
because the semantic-edge stage runs one ``EmbeddingService.search`` per
file. Even with sqlite-vec the cost is dominated by the per-file query
loop, and the panel hits this tool every time the user opens the graph
view. Without a cache, refreshing the panel after a single
``knowledge_update`` triggers a full O(N·K) recomputation that we just
ran milliseconds ago.

Caching strategy:

- Lazy-cron eviction: no background thread, no tasks. Entries carry a
  monotonic timestamp; ``get()`` returns ``None`` when the entry is
  older than ``DEFAULT_TTL_SECONDS``. The next ``set()`` overwrites it.
- TTL is 60s — short enough that a forgotten invalidation hook still
  self-heals quickly, long enough to absorb the panel's polling
  cadence and any double-render hiccups.
- Explicit invalidation by user_id: every write hook
  (``knowledge_update`` / ``knowledge_delete``) calls
  ``invalidate(user_id)``, which drops every cache entry whose key
  belongs to that user. The graph payload depends only on that user's
  knowledge tree, so wiping more than necessary would be wasteful.
- Per-process: this module is global state inside the brain server
  process. The bwrap worker has its own copy, which is correct —
  one brain process per user there means each cache is single-tenant
  by construction.

Threading: the global lock guards both reads and writes. The
``compute_graph`` call holding the cache slot would force every
concurrent panel poll to recompute the same thing while waiting; we
explicitly do NOT lock around the compute step itself, so the worst
case is a brief duplicate compute right after invalidation. That is
strictly faster than serializing every graph fetch behind one mutex.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Hashable

DEFAULT_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class CacheKey:
    """All inputs that affect the graph payload, packed for use as a
    dict key. ``allowed_scopes`` is a sorted tuple (or ``None`` for
    "all scopes") because ``set`` itself isn't hashable."""

    user_id: str | None
    scope: str | None
    include_semantic: bool
    include_tags: bool
    allowed_scopes: tuple[str, ...] | None


def make_key(
    *,
    user_id: str | None,
    scope: str | None,
    include_semantic: bool,
    include_tags: bool,
    allowed_scopes: set[str] | None,
) -> CacheKey:
    """Convenience constructor matching the ``compute_graph`` signature."""
    scopes_tuple: tuple[str, ...] | None
    scopes_tuple = tuple(sorted(allowed_scopes)) if allowed_scopes is not None else None
    return CacheKey(
        user_id=user_id,
        scope=scope,
        include_semantic=include_semantic,
        include_tags=include_tags,
        allowed_scopes=scopes_tuple,
    )


# (payload, expiry_monotonic_seconds)
_cache: dict[CacheKey, tuple[str, float]] = {}
_lock = threading.Lock()
_ttl_seconds = DEFAULT_TTL_SECONDS


def get(key: Hashable) -> str | None:
    """Return the cached payload for ``key`` if it hasn't expired."""
    if not isinstance(key, CacheKey):
        return None
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if now >= expires_at:
            # Lazy eviction — drop the stale entry so the dict doesn't
            # grow unbounded with dead keys when the user's filter
            # combinations change frequently.
            _cache.pop(key, None)
            return None
        return payload


def set(key: CacheKey, payload: str) -> None:
    """Store a freshly-computed payload. Overwrites any prior entry."""
    expires_at = time.monotonic() + _ttl_seconds
    with _lock:
        _cache[key] = (payload, expires_at)


def invalidate(user_id: str | None) -> int:
    """Drop every cache entry belonging to ``user_id`` (matching ``None``
    for the root vault). Returns the number of entries removed.

    Called from the ``knowledge_update`` / ``knowledge_delete`` write
    hooks. We could be more surgical (only invalidate keys whose scope
    matches the touched scope), but a write changes file/edge/relation
    counts globally — semantic edges in particular fan out across
    scopes — so wiping the user's cache fully is both simpler and
    correct.
    """
    removed = 0
    with _lock:
        # ``list()`` because we mutate during iteration.
        for k in list(_cache.keys()):
            if k.user_id == user_id:
                _cache.pop(k, None)
                removed += 1
    return removed


def _set_ttl_for_test(seconds: float) -> None:
    """Test helper — keep the test fast without changing prod TTL."""
    global _ttl_seconds
    _ttl_seconds = seconds


def _reset_for_test() -> None:
    """Test helper — wipe state and restore the default TTL."""
    global _ttl_seconds
    with _lock:
        _cache.clear()
    _ttl_seconds = DEFAULT_TTL_SECONDS


def _size_for_test() -> int:
    """Test helper — current entry count (including expired)."""
    with _lock:
        return len(_cache)
