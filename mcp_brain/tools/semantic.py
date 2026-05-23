"""MCP tool: knowledge_semantic_search — vector-similarity search over chunks."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.embeddings.service import EmbeddingService
from mcp_brain.tools._perms import (
    ALL,
    allowed_subscopes,
    get_current_user_id,
    meter_call,
    require,
)

logger = logging.getLogger(__name__)

_MAX_LIMIT = 30


def register_semantic_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    embedding_service: EmbeddingService | None,
) -> None:
    """Register knowledge_semantic_search.

    If ``embedding_service`` is ``None`` (optional deps missing or
    explicitly disabled) the tool is still registered but returns a
    soft error so clients can detect the feature is off without
    crashing. We don't omit the tool because that would change the
    tool list at runtime in a way the panel UI can't introspect.
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def knowledge_semantic_search(
        query: str,
        limit: int = 10,
        scope: str | None = None,
    ) -> str:
        """DEPRECATED: use `knowledge_search(query, mode='semantic')` instead.

        Kept registered so existing clients (saved Custom Connectors,
        long-lived sessions) keep working. The hybrid default of the
        unified `knowledge_search` will outperform pure semantic for
        most queries — switch unless you specifically need the
        pure-vector behaviour.

        Find knowledge chunks that match the query by semantic similarity.
        Uses sentence embeddings to find chunks whose *meaning* matches
        the query — so "what laptop did I buy" can find the chunk titled
        "Hardware purchases" even without the literal word "laptop".

        Args:
            query: Free-text query. Phrases work better than single
                   keywords because the embedding captures context.
            limit: Maximum number of results (1–30, default 10).
            scope: Optional scope filter (e.g. 'work', 'homelab'). Must
                   be a scope the caller has read access to.
        """
        meter_call("knowledge_semantic_search")

        if not query or not query.strip():
            return "Error: query must not be empty."

        if embedding_service is None:
            return (
                "Semantic search is unavailable on this server. "
                "Install the `embeddings` extra and restart, or unset "
                "`MCP_DISABLE_EMBEDDINGS`."
            )

        limit = max(1, min(_MAX_LIMIT, int(limit)))

        allowed = allowed_subscopes("knowledge:read")
        if scope is not None:
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
            allowed_scopes_filter: set[str] | None = {scope}
        else:
            if allowed is ALL:
                allowed_scopes_filter = None
            else:
                if not allowed:
                    return "No readable knowledge scopes available for this token."
                allowed_scopes_filter = set(allowed)

        user_id = get_current_user_id()

        results = embedding_service.search(
            query,
            k=limit,
            scope=scope,
            allowed_scopes=allowed_scopes_filter,
            user_id=user_id,
        )

        if not results:
            return f"No semantic matches for `{query}`."

        lines: list[str] = [f"## Semantic results for `{query}`\n"]
        for i, hit in enumerate(results, 1):
            preview = hit["text"]
            if len(preview) > 240:
                preview = preview[:237].rstrip() + "…"
            lines.append(
                f"### {i}. {hit['scope']}/{hit['project']} — {hit['heading_path']}"
                f" (distance {hit['distance']:.3f})\n"
            )
            lines.append(f"{preview}\n")
        return "\n".join(lines)
