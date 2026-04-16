"""MCP tools: knowledge_related and knowledge_entities — relationship graph queries."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools._perms import ALL, allowed_subscopes, meter_call, require

logger = logging.getLogger(__name__)

_MAX_DEPTH = 3


def register_graph_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    rel_graph: RelationshipGraph,
) -> None:
    """Register knowledge_related and knowledge_entities tools on the MCP server."""

    @mcp.tool()
    def knowledge_related(
        entity: str,
        depth: int = 1,
        predicate: str | None = None,
        scope: str | None = None,
    ) -> str:
        """Find entities related to a given entity in the knowledge graph.

        Traverses the relationship graph up to `depth` hops and returns the
        entity's neighborhood. Useful for discovering connections between
        concepts, projects, people, and tools across knowledge files.

        Args:
            entity: Entity name to look up (e.g. 'docker', 'school/homelab').
            depth: Number of hops to traverse (1–3, default 1).
            predicate: Optional relationship type filter (e.g. 'mentions',
                       'references', 'has_section').
            scope: Optional knowledge scope to filter results. Must be a scope
                   the caller has read access to.
        """
        meter_call("knowledge_related")

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

        if not entity or not entity.strip():
            return "Error: entity must not be empty."

        depth = max(1, min(depth, _MAX_DEPTH))
        predicates = [predicate] if predicate else None

        info = rel_graph.entity_info(entity)
        if info is None:
            return f"Entity '{entity}' not found in the knowledge graph."

        relations = rel_graph.related(
            entity,
            depth=depth,
            predicates=predicates,
            allowed_scopes=allowed_scopes_filter,
        )

        lines: list[str] = [
            f"## Entity: {info['name']}",
            f"**Type:** {info['entity_type']}  "
            f"**Scope:** {info['scope'] or '—'}  "
            f"**Project:** {info['project'] or '—'}  "
            f"**Relationships:** {info['relationship_count']}",
            "",
        ]

        if not relations:
            lines.append("No related entities found.")
        else:
            lines.append(f"### Related entities (depth={depth})\n")
            for rel in relations:
                source = (
                    f"{rel['scope']}/{rel['project']}"
                    if rel["scope"]
                    else rel["name"]
                )
                lines.append(
                    f"- **{rel['name']}** [{rel['entity_type']}]"
                    f" — `{rel['predicate']}` via `{source}`"
                    f" (confidence {rel['confidence']:.2f}, distance {rel['distance']})"
                )

        return "\n".join(lines)

    @mcp.tool()
    def knowledge_entities(
        scope: str | None = None,
        entity_type: str | None = None,
    ) -> str:
        """List entities in the knowledge graph, optionally filtered by scope or type.

        Shows all known entities (concepts, files, people, tools, etc.) extracted
        from knowledge markdown files, along with their relationship counts.

        Args:
            scope: Optional scope to restrict listing (e.g. 'work', 'school').
                   Must be a scope the caller has read access to.
            entity_type: Optional type filter (e.g. 'concept', 'file', 'section',
                         'reference').
        """
        meter_call("knowledge_entities")

        allowed = allowed_subscopes("knowledge:read")

        if scope is not None:
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
            query_scope: str | None = scope
        else:
            if allowed is not ALL and not allowed:
                return "No readable knowledge scopes available for this token."
            # For non-wildcard tokens without explicit scope, list all allowed.
            # We pass scope=None to list_entities and filter afterwards.
            query_scope = None

        entities = rel_graph.list_entities(scope=query_scope)

        # Apply scope permission filter when no specific scope was requested
        if scope is None and allowed is not ALL:
            entities = [e for e in entities if e["scope"] in allowed]

        # Apply entity_type filter
        if entity_type is not None:
            entities = [e for e in entities if e["entity_type"] == entity_type]

        if not entities:
            return "No entities found."

        lines: list[str] = ["## Knowledge graph entities\n"]
        current_scope = None
        for ent in entities:
            if ent["scope"] != current_scope:
                current_scope = ent["scope"]
                lines.append(f"\n### {current_scope or '(global)'}\n")
            lines.append(
                f"- **{ent['name']}** [{ent['entity_type']}]"
                f" — {ent['relationship_count']} relationship(s)"
                + (f" (`{ent['project']}`)" if ent["project"] else "")
            )

        return "\n".join(lines)
