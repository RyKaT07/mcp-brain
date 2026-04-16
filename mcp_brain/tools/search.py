"""MCP tool: knowledge_search — BM25 full-text search over the knowledge graph."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.search import SearchIndex
from mcp_brain.tools._perms import ALL, allowed_subscopes, meter_call, require

logger = logging.getLogger(__name__)


def register_search_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    search_index: SearchIndex,
) -> None:
    """Register the knowledge_search tool on the MCP server."""

    @mcp.tool()
    def knowledge_search(
        query: str,
        scope: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search across all knowledge files using full-text search.
        Returns BM25-ranked results with snippets.

        Args:
            query: Full-text search query (supports phrase search, e.g. "gate driver")
            scope: Optional scope to restrict search (e.g. 'work', 'school').
                   Must be a scope the caller has read access to.
            limit: Maximum number of results to return (default 10).
        """
        meter_call("knowledge_search")

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

        results = search_index.search(query, allowed_scopes_filter, limit=limit)

        if not results:
            return "No results found."

        lines: list[str] = [f"## Search results for `{query}`\n"]
        for i, hit in enumerate(results, 1):
            lines.append(
                f"### {i}. {hit['scope']}/{hit['project']} — {hit['section']}\n"
                f"{hit['snippet']}\n"
            )

        return "\n".join(lines)
