"""MCP tools: knowledge_related and knowledge_entities — relationship graph queries."""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools._perms import ALL, allowed_subscopes, get_current_user_id, meter_call, require

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
        as_of: str | None = None,
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
            as_of: Optional ISO 8601 datetime string. When set, only returns
                   relationships that were valid at that point in time
                   (based on valid_from / valid_to columns).
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

        # Resolve per-user graph (lazy build on first access).
        user_id = get_current_user_id()
        user_graph: RelationshipGraph | None = None
        if user_id is not None:
            user_dir = knowledge_dir / "users" / user_id
            if not rel_graph.has_user_graph(user_id) and user_dir.is_dir():
                rel_graph.build_user(user_id, user_dir)
            user_graph = rel_graph.get_user_graph(user_id)

        # Look up entity info in global graph first, then user graph.
        info = rel_graph.entity_info(entity)
        user_info = user_graph.entity_info(entity) if user_graph is not None else None

        if info is None and user_info is None:
            return f"Entity '{entity}' not found in the knowledge graph."

        # Display whichever info we found (prefer user's own entity if it exists).
        display_info = user_info if user_info is not None else info

        relations: list[dict] = []
        if info is not None:
            relations = rel_graph.related(
                entity,
                depth=depth,
                predicates=predicates,
                allowed_scopes=allowed_scopes_filter,
                as_of=as_of,
            )

        # Merge user-graph relations (deduplicated by name).
        if user_graph is not None and user_info is not None:
            user_relations = user_graph.related(
                entity,
                depth=depth,
                predicates=predicates,
                allowed_scopes=allowed_scopes_filter,
                as_of=as_of,
            )
            seen_names = {r["name"] for r in relations}
            for r in user_relations:
                if r["name"] not in seen_names:
                    relations.append(r)
                    seen_names.add(r["name"])

        lines: list[str] = [
            f"## Entity: {display_info['name']}",
            f"**Type:** {display_info['entity_type']}  "
            f"**Scope:** {display_info['scope'] or '—'}  "
            f"**Project:** {display_info['project'] or '—'}  "
            f"**Relationships:** {display_info['relationship_count']}",
            "",
        ]
        if as_of:
            lines.append(f"*Temporal filter: as of {as_of}*\n")

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

        # Merge entities from the per-user graph (lazy build on first access).
        user_id = get_current_user_id()
        if user_id is not None:
            user_dir = knowledge_dir / "users" / user_id
            if not rel_graph.has_user_graph(user_id) and user_dir.is_dir():
                rel_graph.build_user(user_id, user_dir)
            user_graph = rel_graph.get_user_graph(user_id)
            if user_graph is not None:
                user_entities = user_graph.list_entities(scope=query_scope)
                if scope is None and allowed is not ALL:
                    user_entities = [e for e in user_entities if e["scope"] in allowed]
                # Deduplicate by (name, scope, project) key
                seen = {(e["name"], e["scope"], e["project"]) for e in entities}
                for ue in user_entities:
                    key = (ue["name"], ue["scope"], ue["project"])
                    if key not in seen:
                        entities.append(ue)
                        seen.add(key)

        # Apply entity_type filter
        if entity_type is not None:
            entities = [e for e in entities if e["entity_type"] == entity_type]

        if not entities:
            return "No entities found."

        lines: list[str] = ["## Knowledge graph entities\n"]
        current_scope = None
        for ent in sorted(entities, key=lambda e: (e["scope"], e["project"], e["name"])):
            if ent["scope"] != current_scope:
                current_scope = ent["scope"]
                lines.append(f"\n### {current_scope or '(global)'}\n")
            lines.append(
                f"- **{ent['name']}** [{ent['entity_type']}]"
                f" — {ent['relationship_count']} relationship(s)"
                + (f" (`{ent['project']}`)" if ent["project"] else "")
            )

        return "\n".join(lines)

    @mcp.tool()
    def knowledge_timeline(
        entity: str,
        scope: str | None = None,
    ) -> str:
        """Show how an entity's relationships evolved over time.

        Returns all relationships involving the entity, sorted by when they
        were first observed (most recent first). Useful for understanding
        how a concept, project, or person's connections have changed.

        Args:
            entity: Entity name to look up (e.g. 'docker', 'school/homelab').
            scope: Optional knowledge scope for permission checking. Must be a
                   scope the caller has read access to.
        """
        meter_call("knowledge_timeline")

        allowed = allowed_subscopes("knowledge:read")

        if scope is not None:
            try:
                require(f"knowledge:read:{scope}")
            except PermissionDenied as e:
                return str(e)
        else:
            if allowed is not ALL and not allowed:
                return "No readable knowledge scopes available for this token."

        if not entity or not entity.strip():
            return "Error: entity must not be empty."

        # Resolve per-user graph (lazy build).
        user_id = get_current_user_id()
        user_graph: RelationshipGraph | None = None
        if user_id is not None:
            user_dir = knowledge_dir / "users" / user_id
            if not rel_graph.has_user_graph(user_id) and user_dir.is_dir():
                rel_graph.build_user(user_id, user_dir)
            user_graph = rel_graph.get_user_graph(user_id)

        info = rel_graph.entity_info(entity)
        user_info = user_graph.entity_info(entity) if user_graph is not None else None

        if info is None and user_info is None:
            return f"Entity '{entity}' not found in the knowledge graph."

        display_info = user_info if user_info is not None else info

        entries = rel_graph.timeline(entity) if info is not None else []
        if user_graph is not None and user_info is not None:
            user_entries = user_graph.timeline(entity)
            entries = entries + user_entries
            entries.sort(key=lambda e: e.get("observed_at") or "", reverse=True)

        lines: list[str] = [
            f"## Timeline: {display_info['name']}",
            f"**Type:** {display_info['entity_type']}  "
            f"**Scope:** {display_info['scope'] or '—'}  "
            f"**Project:** {display_info['project'] or '—'}",
            "",
        ]

        if not entries:
            lines.append("No relationship history found.")
        else:
            lines.append(f"### Relationships ({len(entries)} total, newest first)\n")
            for entry in entries:
                ts = entry["observed_at"] or "unknown"
                validity = ""
                if entry["valid_from"] or entry["valid_to"]:
                    vf = entry["valid_from"] or "∞"
                    vt = entry["valid_to"] or "now"
                    validity = f" [valid {vf} → {vt}]"
                lines.append(
                    f"- `{entry['subject']}` —[{entry['predicate']}]→ `{entry['object']}`"
                    f" (observed {ts}{validity},"
                    f" confidence {entry['confidence']:.2f},"
                    f" source `{entry['source_scope']}/{entry['source_project']}`)"
                )

        return "\n".join(lines)
