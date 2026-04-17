"""MCP tool: knowledge_search — BM25 full-text search over the knowledge graph."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.search import SearchIndex
from mcp_brain.tools._perms import ALL, allowed_subscopes, get_current_user_id, meter_call, require

logger = logging.getLogger(__name__)

_VALID_SOURCES = {"knowledge", "todoist", "trello"}
_SOURCE_LABEL = {"todoist": " [todoist]", "trello": " [trello]", "knowledge": ""}


def register_search_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    search_index: SearchIndex,
) -> None:
    """Register the knowledge_search tool on the MCP server."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def knowledge_search(
        query: str,
        scope: str | None = None,
        source: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search across all knowledge files, Todoist tasks, and Trello cards
        using full-text search. Returns BM25-ranked results with snippets.

        Args:
            query: Full-text search query (supports phrase search, e.g. "gate driver")
            scope: Optional scope to restrict search (e.g. 'work', 'school').
                   Must be a scope the caller has read access to.
            source: Optional source filter. One of "knowledge", "todoist", or
                    "trello". If omitted, results from all sources are returned.
            limit: Maximum number of results to return (default 10).
        """
        meter_call("knowledge_search")

        if source is not None and source not in _VALID_SOURCES:
            return f"Error: source must be one of: {', '.join(sorted(_VALID_SOURCES))}. Got: {source!r}"

        # Determine which scopes the caller may read
        allowed = allowed_subscopes("knowledge:read")

        if scope is not None:
            # Caller requested a specific scope — verify access
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
            allowed_scopes_filter: list[str] | None = [scope]
        else:
            # No scope filter requested — search all allowed scopes
            if allowed is ALL:
                allowed_scopes_filter = None  # no filter — search everything
            else:
                if not allowed:
                    return "No readable knowledge scopes available for this token."
                allowed_scopes_filter = list(allowed)

        if not query or not query.strip():
            return "Error: query must not be empty."

        results = search_index.search(query, allowed_scopes_filter, limit=limit, source=source)

        # Also search the per-user index when a user context is active.
        # The user's files live in knowledge/users/{user_id}/ and are indexed
        # separately so they never contaminate the global (shared) index.
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

        if not results:
            return "No results found."

        lines: list[str] = [f"## Search results for `{query}`\n"]
        for i, hit in enumerate(results, 1):
            src_label = _SOURCE_LABEL.get(hit.get("source", "knowledge"), "")
            lines.append(
                f"### {i}. {hit['scope']}/{hit['project']} — {hit['section']}{src_label}\n"
                f"{hit['snippet']}\n"
            )

        return "\n".join(lines)
