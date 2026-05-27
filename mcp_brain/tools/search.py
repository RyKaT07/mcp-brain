"""MCP tool: knowledge_search — hybrid (BM25 + semantic) search over the knowledge graph."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.embeddings.service import EmbeddingService
from mcp_brain.search import SearchIndex
from mcp_brain.tools._perms import ALL, allowed_subscopes, get_current_user_id, meter_call, require

logger = logging.getLogger(__name__)

_VALID_SOURCES = {"knowledge", "todoist", "trello"}
_SOURCE_LABEL = {"todoist": " [todoist]", "trello": " [trello]", "knowledge": ""}
_VALID_MODES = {"hybrid", "bm25", "semantic"}
# RRF constant. 60 is the de-facto default from the original paper and
# Elasticsearch's `rank_constant`; values in [10, 100] all behave
# reasonably and the score itself is never surfaced to the model — only
# the ordering matters.
_RRF_K = 60


def _run_bm25(
    search_index: SearchIndex,
    knowledge_dir: Path,
    query: str,
    allowed_scopes_filter: list[str] | None,
    source: str | None,
    limit: int,
) -> list[dict]:
    """Shared BM25 path: global index plus the per-user index when present."""
    results = search_index.search(
        query, allowed_scopes_filter, limit=limit, source=source
    )

    user_id = get_current_user_id()
    if user_id is not None:
        user_dir = knowledge_dir / "users" / user_id
        if not search_index.has_user_index(user_id) and user_dir.is_dir():
            search_index.build_user(user_id, user_dir)
        user_idx = search_index.get_user_index(user_id)
        if user_idx is not None:
            user_results = user_idx.search(
                query, allowed_scopes_filter, limit=limit, source=source
            )
            if user_results:
                results = results + user_results
                results.sort(key=lambda r: r["rank"])
                results = results[:limit]

    return results


def _run_semantic(
    embedding_service: EmbeddingService,
    query: str,
    allowed_scopes_filter: set[str] | None,
    limit: int,
) -> list[dict]:
    """Shared semantic path. Embedding failures degrade to an empty list
    so a transient model error never blocks the BM25 leg of a hybrid call."""
    try:
        return embedding_service.search(
            query,
            k=limit,
            allowed_scopes=allowed_scopes_filter,
            user_id=get_current_user_id(),
        )
    except Exception as exc:
        logger.warning("semantic search failed: %s", exc)
        return []


def _fuse(bm25_hits: list[dict], sem_hits: list[dict], limit: int) -> list[dict]:
    """Reciprocal-rank-fuse two ranked lists at the (scope, project, section) grain.

    Snippet + section come from whichever leg ranked the chunk higher;
    we always prefer the BM25 snippet when available because its
    surrounding-text excerpt is more readable than the raw semantic
    chunk body.
    """
    scores: dict[tuple[str, str, str], float] = {}
    chosen: dict[tuple[str, str, str], dict] = {}

    for rank, h in enumerate(bm25_hits, start=1):
        key = (h["scope"], h["project"], h.get("section", ""))
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
        chosen.setdefault(key, h)

    for rank, h in enumerate(sem_hits, start=1):
        section = h.get("heading_path", "") or ""
        # Drop the leading "## " if the embedding store stored it that way
        # — BM25 hits surface bare H2 titles, normalise so both legs land
        # under the same key when they point at the same chunk.
        section_norm = section.lstrip("# ").strip()
        key = (h["scope"], h["project"], section_norm)
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
        if key not in chosen:
            text = h.get("text", "")
            preview = text[:240].rstrip()
            if len(text) > 240:
                preview += "…"
            chosen[key] = {
                "scope": h["scope"],
                "project": h["project"],
                "section": section_norm,
                "snippet": preview,
                "source": "knowledge",
            }

    ranked_keys = sorted(scores, key=lambda k: scores[k], reverse=True)
    return [chosen[k] for k in ranked_keys[:limit]]


def register_search_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    search_index: SearchIndex,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Register the knowledge_search tool on the MCP server."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def knowledge_search(
        query: str,
        scope: str | None = None,
        source: str | None = None,
        limit: int = 10,
        mode: str = "hybrid",
    ) -> str:
        """Search across knowledge files (and optionally Todoist / Trello).

        Default mode is **hybrid**: results from BM25 full-text and from
        embedding-based semantic search are merged via reciprocal rank
        fusion. Hybrid gives you keyword precision (BM25 is unbeatable
        for exact phrase / identifier lookups) and semantic recall
        (matches the *meaning* of the query, e.g. "what laptop did I
        buy" finds a chunk titled "Hardware purchases" without the
        literal word "laptop") in a single call.

        Args:
            query: Free-text query. Both legs accept the same string;
                   BM25 also supports phrase search (e.g. `"gate driver"`).
            scope: Optional scope to restrict to (e.g. 'work', 'school').
                   Must be a scope the caller has read access to.
            source: Optional source filter. One of "knowledge", "todoist",
                    or "trello". Applies to the BM25 leg only (semantic
                    only indexes knowledge). Semantic is skipped when a
                    non-knowledge source is requested.
            limit: Maximum number of results to return (default 10).
            mode: "hybrid" (default), "bm25", or "semantic". "bm25"
                  preserves the legacy text-only behaviour; "semantic"
                  replaces the deprecated knowledge_semantic_search.
        """
        meter_call("knowledge_search")

        if source is not None and source not in _VALID_SOURCES:
            return f"Error: source must be one of: {', '.join(sorted(_VALID_SOURCES))}. Got: {source!r}"

        if mode not in _VALID_MODES:
            return f"Error: mode must be one of: {', '.join(sorted(_VALID_MODES))}. Got: {mode!r}"

        # Determine which scopes the caller may read
        allowed = allowed_subscopes("knowledge:read")

        if scope is not None:
            # Caller requested a specific scope — verify access
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
            allowed_scopes_filter: list[str] | None = [scope]
            sem_scopes_filter: set[str] | None = {scope}
        else:
            # No scope filter requested — search all allowed scopes
            if allowed is ALL:
                allowed_scopes_filter = None
                sem_scopes_filter = None
            else:
                if not allowed:
                    return "No readable knowledge scopes available for this token."
                allowed_scopes_filter = list(allowed)
                sem_scopes_filter = set(allowed)

        if not query or not query.strip():
            return "Error: query must not be empty."

        # Decide which legs to run. Semantic doesn't apply when the
        # caller filtered to todoist/trello, so silently drop it from
        # hybrid in that case rather than returning nothing.
        run_bm25 = mode in {"hybrid", "bm25"}
        run_sem = (
            mode in {"hybrid", "semantic"}
            and embedding_service is not None
            and source in (None, "knowledge")
        )

        if mode == "semantic" and embedding_service is None:
            return (
                "Semantic search is unavailable on this server. "
                "Install the `embeddings` extra and restart, or use "
                "`mode='bm25'`."
            )

        bm25_hits: list[dict] = []
        sem_hits: list[dict] = []

        if run_bm25:
            # Over-fetch so the fusion has headroom to re-order; the
            # final list is truncated to `limit` by `_fuse`.
            bm25_limit = limit * 2 if run_sem else limit
            bm25_hits = _run_bm25(
                search_index, knowledge_dir, query, allowed_scopes_filter, source, bm25_limit
            )

        if run_sem:
            sem_limit = limit * 2 if run_bm25 else limit
            sem_hits = _run_semantic(
                embedding_service, query, sem_scopes_filter, sem_limit
            )

        if run_bm25 and run_sem:
            results = _fuse(bm25_hits, sem_hits, limit)
            mode_label = "hybrid"
        elif run_sem:
            results = _fuse([], sem_hits, limit)
            mode_label = "semantic"
        else:
            results = bm25_hits[:limit]
            mode_label = "bm25"

        if not results:
            return "No results found."

        lines: list[str] = [f"## Search results for `{query}` ({mode_label})\n"]
        for i, hit in enumerate(results, 1):
            src_label = _SOURCE_LABEL.get(hit.get("source", "knowledge"), "")
            section = hit.get("section", "")
            snippet = hit.get("snippet", "")
            lines.append(
                f"### {i}. {hit['scope']}/{hit['project']} — {section}{src_label}\n"
                f"{snippet}\n"
            )

        return "\n".join(lines)
