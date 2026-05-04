"""MCP tools: knowledge_layout_get / _set / _clear — persisted graph positions.

The panel's force-directed graph layout is non-trivial to recompute on
every page load and was previously cached only in localStorage. That
made the layout per-browser, and lost it whenever a different machine
or a different user opened the same vault. Persisting positions in the
brain means the panel renders the same shape from any client and
across sessions.

Coordinates are kept in the same 0..1000 viewBox space the panel
renders into, so neither side has to rescale.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools._perms import (
    ALL,
    allowed_subscopes,
    get_current_user_id,
    meter_call,
    require,
)

logger = logging.getLogger(__name__)


def _resolve_layout_graph(
    rel_graph: RelationshipGraph,
    knowledge_dir: Path,
    user_id: str | None,
) -> RelationshipGraph:
    """Pick the right ``RelationshipGraph`` instance for layout I/O.

    Patryk's single-user setup writes to the root graph. Multi-user
    setups (yaml token with ``user_id``, OAuth) write to the per-user
    graph so layouts stay isolated. The user graph is built lazily on
    first access; if the user has no knowledge dir yet we fall back to
    the root graph rather than create an empty per-user DB.
    """
    if user_id is None:
        return rel_graph
    user_dir = knowledge_dir / "users" / user_id
    if not rel_graph.has_user_graph(user_id) and user_dir.is_dir():
        rel_graph.build_user(user_id, user_dir)
    user_graph = rel_graph.get_user_graph(user_id)
    return user_graph if user_graph is not None else rel_graph


def register_layout_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    rel_graph: RelationshipGraph,
) -> None:
    """Register knowledge_layout_get / knowledge_layout_set / knowledge_layout_clear."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def knowledge_layout_get(scope: str | None = None) -> str:
        """Return persisted graph node positions as JSON.

        Output: ``{"positions": [{"scope", "project", "x", "y", "updated_at"}, ...]}``.

        Used by the panel's Graph view to render nodes from a stable
        position. If no layout is stored yet the list is empty and the
        panel falls back to its in-browser force-directed compute.

        Args:
            scope: Optional scope filter; only positions in that scope
                   are returned. Must be a scope the caller can read.
        """
        meter_call("knowledge_layout_get")

        allowed = allowed_subscopes("knowledge:read")
        if scope is not None:
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return json.dumps({"error": str(e)})
            allowed_scopes_filter: set[str] | None = {scope}
        else:
            if allowed is ALL:
                allowed_scopes_filter = None
            else:
                if not allowed:
                    return json.dumps({"error": "No readable knowledge scopes available."})
                allowed_scopes_filter = set(allowed)

        graph = _resolve_layout_graph(rel_graph, knowledge_dir, get_current_user_id())
        positions = graph.get_layout(allowed_scopes=allowed_scopes_filter)
        return json.dumps({"positions": positions})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def knowledge_layout_set(positions: list[dict]) -> str:
        """Upsert graph node positions.

        Each entry must include ``scope``, ``project``, ``x``, ``y``.
        Coordinates are in the panel's 0..1000 viewBox space.

        Returns ``{"written": <int>}`` — the count of valid rows that
        were upserted. Rows missing required keys are silently skipped
        so a partial bad payload doesn't lose the rest.

        Permission: caller needs ``knowledge:write`` on every scope
        appearing in ``positions``. Mixed payloads with at least one
        unauthorised scope are rejected with an error.
        """
        meter_call("knowledge_layout_set")

        if not isinstance(positions, list):
            return json.dumps({"error": "positions must be a list."})

        # Permission check: every scope in the payload must be writable.
        scopes_in_payload = {
            p["scope"] for p in positions
            if isinstance(p, dict) and isinstance(p.get("scope"), str)
        }
        for s in scopes_in_payload:
            try:
                require(f"knowledge:write:{s}")
            except PermissionDenied as e:
                return json.dumps({"error": str(e)})

        graph = _resolve_layout_graph(rel_graph, knowledge_dir, get_current_user_id())
        written = graph.set_layout(positions)
        return json.dumps({"written": written})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def knowledge_layout_clear() -> str:
        """Drop every persisted node position.

        Used when the panel's "↻ relayout" pill is pressed and the
        client wants the brain to forget the cached layout so the next
        ``knowledge_layout_set`` writes a freshly computed one.

        Permission: ``knowledge:write`` (any scope). Returns
        ``{"removed": <int>}``.
        """
        meter_call("knowledge_layout_clear")

        # Single broad write check — clear is all-or-nothing.
        allowed = allowed_subscopes("knowledge:write")
        if allowed is not ALL and not allowed:
            return json.dumps(
                {"error": "No writable knowledge scopes available for this token."}
            )

        graph = _resolve_layout_graph(rel_graph, knowledge_dir, get_current_user_id())
        removed = graph.clear_layout()
        return json.dumps({"removed": removed})
