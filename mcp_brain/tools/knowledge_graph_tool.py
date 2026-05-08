"""MCP tool: knowledge_graph — full graph (nodes + edges) for the panel."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.embeddings.service import EmbeddingService
from mcp_brain import graph_cache
from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools._perms import (
    ALL,
    allowed_subscopes,
    get_current_user_id,
    meter_call,
    require,
)

logger = logging.getLogger(__name__)


# Edge weights — calibrated so a force-directed layout pulls explicit
# links tighter than tag co-occurrence and tag pairs tighter than vector
# neighbours. Tweak as needed once we have user feedback.
_WEIGHT_LINK = 1.0          # [[wikilinks]] / @backlinks / file refs
_WEIGHT_TAG = 0.5           # two files sharing a #tag
_WEIGHT_SEMANTIC_MAX = 0.6  # cosine top-K (multiplied by similarity)

# Semantic distance ceiling — ignore matches above this. sqlite-vec
# returns L2 distance; for unit-normalised embeddings 0.0 means
# identical and 2.0 means antipodal. We accept up to 1.0.
_SEMANTIC_DISTANCE_MAX = 1.0
_SEMANTIC_K_PER_FILE = 3


def _file_node_id(scope: str, project: str) -> str:
    return f"{scope}/{project}"


def _build_tag_coedges(
    relationships: list[dict],
) -> list[tuple[str, str, float]]:
    """Derive file↔file edges from shared tags.

    Each tag links every pair of files that uses it; weight accumulates
    when two files share multiple tags. Tags used by only one file
    contribute no edges.
    """
    files_by_tag: dict[str, set[str]] = defaultdict(set)
    for r in relationships:
        if r["predicate"] != "tagged":
            continue
        if r["subject_type"] != "file":
            continue
        tag = r["object_name"]
        file_id = _file_node_id(r["subject_scope"], r["subject_project"])
        files_by_tag[tag].add(file_id)

    edges: dict[tuple[str, str], float] = {}
    for files in files_by_tag.values():
        items = sorted(files)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                key = (items[i], items[j])
                edges[key] = edges.get(key, 0.0) + _WEIGHT_TAG
    return [(a, b, w) for (a, b), w in edges.items()]


def _build_semantic_edges(
    embedding_service: EmbeddingService | None,
    file_nodes: list[dict],
    user_id: str | None,
    allowed_scopes: set[str] | None,
) -> list[tuple[str, str, float]]:
    """Top-K semantic neighbours per file, file↔file only.

    Uses each file's first 600 chars as a query proxy — the preamble
    plus first heading, which captures the file's topical centre. F3
    will move to denser per-chunk queries if visual clusters need
    sharper boundaries.
    """
    if embedding_service is None:
        return []
    knowledge_dir = embedding_service.knowledge_dir
    edges: dict[tuple[str, str], float] = {}
    for node in file_nodes:
        scope = node["scope"]
        project = node["project"]
        md_path = knowledge_dir / scope / f"{project}.md"
        if not md_path.exists():
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        query = text.strip()[:600]
        if not query:
            continue
        try:
            hits = embedding_service.search(
                query,
                k=_SEMANTIC_K_PER_FILE + 1,  # +1 because the file itself shows up
                allowed_scopes=allowed_scopes,
                user_id=user_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "semantic edge build: search failed for %s/%s",
                scope,
                project,
                exc_info=True,
            )
            continue
        src = _file_node_id(scope, project)
        for hit in hits:
            dst = _file_node_id(hit["scope"], hit["project"])
            if dst == src:
                continue
            distance = float(hit["distance"])
            if distance > _SEMANTIC_DISTANCE_MAX:
                continue
            similarity = max(0.0, 1.0 - distance / 2.0)
            weight = round(_WEIGHT_SEMANTIC_MAX * similarity, 3)
            if weight <= 0:
                continue
            key = (src, dst) if src < dst else (dst, src)
            existing = edges.get(key, 0.0)
            if weight > existing:
                edges[key] = weight
    return [(a, b, w) for (a, b), w in edges.items()]


def compute_graph(
    knowledge_dir: Path,
    rel_graph: RelationshipGraph,
    embedding_service: EmbeddingService | None,
    *,
    scope: str | None = None,
    include_semantic: bool = True,
    include_tags: bool = True,
    allowed_scopes_filter: set[str] | None = None,
    user_id: str | None = None,
) -> str:
    """Pure compute: return the JSON-encoded graph payload.

    Factored out of the tool wrapper so tests can call it directly
    without going through the FastMCP harness (which is stubbed in
    the conftest, hiding the inner closure).
    """
    user_graph: RelationshipGraph | None = None
    if user_id is not None:
        user_dir = knowledge_dir / "users" / user_id
        if not rel_graph.has_user_graph(user_id) and user_dir.is_dir():
            rel_graph.build_user(user_id, user_dir)
        user_graph = rel_graph.get_user_graph(user_id)

    files = rel_graph.list_files(allowed_scopes=allowed_scopes_filter)
    seen_ids = {_file_node_id(f["scope"], f["project"]) for f in files}
    if user_graph is not None:
        for f in user_graph.list_files(allowed_scopes=allowed_scopes_filter):
            fid = _file_node_id(f["scope"], f["project"])
            if fid not in seen_ids:
                files.append(f)
                seen_ids.add(fid)

    nodes: list[dict] = []
    for f in files:
        entity_info = rel_graph.entity_info(f["name"])
        count = entity_info["relationship_count"] if entity_info else 0
        nodes.append(
            {
                "id": _file_node_id(f["scope"], f["project"]),
                "scope": f["scope"],
                "project": f["project"],
                "relationship_count": count,
            }
        )

    link_predicates = ["mentions", "references"]
    all_relations = rel_graph.all_relationships(
        allowed_scopes=allowed_scopes_filter
    )
    if user_graph is not None:
        all_relations.extend(
            user_graph.all_relationships(allowed_scopes=allowed_scopes_filter)
        )

    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    node_ids = {n["id"] for n in nodes}

    # Link edges: file → file (or file → reference that resolves to a file).
    # Concept-only mentions (e.g. [[some-idea]]) don't surface as nodes,
    # so we skip them here to keep the visual signal focused on files.
    for r in all_relations:
        if r["predicate"] not in link_predicates:
            continue
        if r["subject_type"] != "file":
            continue
        if r["object_type"] not in ("file", "reference"):
            continue
        src = _file_node_id(r["subject_scope"], r["subject_project"])
        dst = r["object_name"]
        if src == dst:
            continue
        if src not in node_ids or dst not in node_ids:
            continue
        key = (src, dst, "link") if src < dst else (dst, src, "link")
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            {
                "src": key[0],
                "dst": key[1],
                "kind": "link",
                "weight": _WEIGHT_LINK,
            }
        )

    if include_tags:
        for src, dst, weight in _build_tag_coedges(all_relations):
            if src not in node_ids or dst not in node_ids:
                continue
            key = (src, dst, "tag") if src < dst else (dst, src, "tag")
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                {
                    "src": key[0],
                    "dst": key[1],
                    "kind": "tag",
                    "weight": round(weight, 3),
                }
            )

    if include_semantic and embedding_service is not None:
        for src, dst, weight in _build_semantic_edges(
            embedding_service, nodes, user_id, allowed_scopes_filter
        ):
            if src not in node_ids or dst not in node_ids:
                continue
            key = (
                (src, dst, "semantic") if src < dst else (dst, src, "semantic")
            )
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                {
                    "src": key[0],
                    "dst": key[1],
                    "kind": "semantic",
                    "weight": weight,
                }
            )

    return json.dumps(
        {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "edges_by_kind": {
                    kind: sum(1 for e in edges if e["kind"] == kind)
                    for kind in ("link", "tag", "semantic")
                },
            },
        },
        ensure_ascii=False,
        indent=None,
    )


def register_knowledge_graph_tool(
    mcp: FastMCP,
    knowledge_dir: Path,
    rel_graph: RelationshipGraph,
    embedding_service: EmbeddingService | None,
) -> None:
    """Register the ``knowledge_graph`` tool on the MCP server."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def knowledge_graph(
        scope: str | None = None,
        include_semantic: bool = True,
        include_tags: bool = True,
    ) -> str:
        """Return the full knowledge graph (nodes + edges) as JSON.

        The output is a single JSON object with ``nodes`` and
        ``edges``. Nodes are file entities with ``{id, scope,
        project, relationship_count}``. Edges are file↔file pairs with
        ``{src, dst, kind, weight}`` where ``kind`` is ``link``,
        ``tag``, or ``semantic``. The frontend renders a force-directed
        layout — server doesn't compute positions.

        Args:
            scope: Optional scope filter. Must be a scope the caller
                   has read access to.
            include_semantic: When True, add file↔file edges derived
                              from top-K cosine neighbours over chunk
                              embeddings. Skipped silently if the
                              embedding subsystem isn't enabled.
            include_tags: When True, add file↔file edges derived from
                          shared ``#tag`` hashtags.
        """
        meter_call("knowledge_graph")

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
                    return json.dumps(
                        {"error": "No readable knowledge scopes available."}
                    )
                allowed_scopes_filter = set(allowed)

        user_id = get_current_user_id()
        cache_key = graph_cache.make_key(
            user_id=user_id,
            scope=scope,
            include_semantic=include_semantic,
            include_tags=include_tags,
            allowed_scopes=allowed_scopes_filter,
        )
        cached = graph_cache.get(cache_key)
        if cached is not None:
            return cached

        payload = compute_graph(
            knowledge_dir,
            rel_graph,
            embedding_service,
            scope=scope,
            include_semantic=include_semantic,
            include_tags=include_tags,
            allowed_scopes_filter=allowed_scopes_filter,
            user_id=user_id,
        )
        graph_cache.set(cache_key, payload)
        return payload
