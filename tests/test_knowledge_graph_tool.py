"""Tests for tag extraction in graph.py and the knowledge_graph MCP tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_brain.graph import RelationshipGraph
from mcp_brain.tools.knowledge_graph_tool import (
    _build_tag_coedges,
    _file_node_id,
    compute_graph,
)


def _make_md(tmp_path: Path, scope: str, project: str, content: str) -> Path:
    d = tmp_path / scope
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{project}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ── Tag extraction ──────────────────────────────────────────────────


class TestTagExtraction:
    def test_simple_hashtag_creates_tag_entity(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "Some text with #python tag.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        ents = g.list_entities()
        names = {e["name"]: e["entity_type"] for e in ents}
        assert names.get("python") == "tag"

    def test_h2_heading_does_not_match_as_tag(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "## Section Title\nbody")
        g = RelationshipGraph()
        g.build(tmp_path)
        names = {e["name"] for e in g.list_entities()}
        # No "section" tag — H2 must not be picked up by the tag regex.
        assert "section" not in {n for n in names if "/" not in n}

    def test_multiple_tags_in_one_file(self, tmp_path):
        _make_md(
            tmp_path, "work", "doc", "Notes #python #django and #testing.\n"
        )
        g = RelationshipGraph()
        g.build(tmp_path)
        types = {e["name"]: e["entity_type"] for e in g.list_entities()}
        for tag in ("python", "django", "testing"):
            assert types.get(tag) == "tag"

    def test_tag_lowercased(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "Note #Python here.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        names = {e["name"] for e in g.list_entities() if e["entity_type"] == "tag"}
        assert "python" in names
        assert "Python" not in names

    def test_repeated_tag_in_one_file_dedupes(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "#python #python again #python\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/doc", depth=1)
        py_relations = [r for r in related if r["name"] == "python"]
        # File → tag relation appears at most once for the file.
        assert len(py_relations) == 1

    def test_tag_creates_tagged_relationship(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "Using #django here.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/doc", depth=1)
        rel = next((r for r in related if r["name"] == "django"), None)
        assert rel is not None
        assert rel["predicate"] == "tagged"


# ── all_relationships query ─────────────────────────────────────────


class TestAllRelationships:
    def test_returns_link_and_tag_rows(self, tmp_path):
        _make_md(
            tmp_path,
            "work",
            "doc",
            "See [[concept]] and #python tag.\n",
        )
        g = RelationshipGraph()
        g.build(tmp_path)
        rows = g.all_relationships()
        predicates = {r["predicate"] for r in rows}
        assert "mentions" in predicates
        assert "tagged" in predicates

    def test_predicate_filter(self, tmp_path):
        _make_md(tmp_path, "work", "doc", "[[concept]] and #tag\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        rows = g.all_relationships(predicates=["tagged"])
        assert {r["predicate"] for r in rows} == {"tagged"}

    def test_allowed_scopes_filter(self, tmp_path):
        _make_md(tmp_path, "work", "a", "[[x]]")
        _make_md(tmp_path, "school", "b", "[[y]]")
        g = RelationshipGraph()
        g.build(tmp_path)
        rows = g.all_relationships(allowed_scopes={"work"})
        assert all(r["source_scope"] == "work" for r in rows)


# ── list_files ──────────────────────────────────────────────────────


class TestListFiles:
    def test_returns_files_only(self, tmp_path):
        _make_md(tmp_path, "work", "alpha", "[[x]]\n")
        _make_md(tmp_path, "school", "beta", "[[y]]\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        files = g.list_files()
        names = sorted(f["name"] for f in files)
        assert "work/alpha" in names
        assert "school/beta" in names

    def test_allowed_scopes_filter(self, tmp_path):
        _make_md(tmp_path, "work", "a", "x\n")
        _make_md(tmp_path, "school", "b", "y\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        files = g.list_files(allowed_scopes={"work"})
        assert {f["scope"] for f in files} == {"work"}


# ── Tag co-edge derivation ──────────────────────────────────────────


class TestTagCoedges:
    def test_two_files_sharing_tag_create_one_edge(self):
        relationships = [
            {
                "predicate": "tagged",
                "subject_type": "file",
                "subject_scope": "work",
                "subject_project": "a",
                "object_name": "python",
            },
            {
                "predicate": "tagged",
                "subject_type": "file",
                "subject_scope": "work",
                "subject_project": "b",
                "object_name": "python",
            },
        ]
        edges = _build_tag_coedges(relationships)
        assert len(edges) == 1
        a, b, w = edges[0]
        assert {a, b} == {"work/a", "work/b"}
        assert w == pytest.approx(0.5)

    def test_three_files_sharing_tag_create_three_edges(self):
        relationships = [
            {
                "predicate": "tagged",
                "subject_type": "file",
                "subject_scope": "work",
                "subject_project": p,
                "object_name": "python",
            }
            for p in ("a", "b", "c")
        ]
        edges = _build_tag_coedges(relationships)
        assert len(edges) == 3  # C(3, 2)

    def test_shared_multiple_tags_accumulate_weight(self):
        rels = []
        for tag in ("python", "django"):
            for project in ("a", "b"):
                rels.append(
                    {
                        "predicate": "tagged",
                        "subject_type": "file",
                        "subject_scope": "work",
                        "subject_project": project,
                        "object_name": tag,
                    }
                )
        edges = _build_tag_coedges(rels)
        assert len(edges) == 1
        _, _, w = edges[0]
        assert w == pytest.approx(1.0)  # 0.5 + 0.5

    def test_tag_on_one_file_only_yields_no_edge(self):
        relationships = [
            {
                "predicate": "tagged",
                "subject_type": "file",
                "subject_scope": "work",
                "subject_project": "lonely",
                "object_name": "rare-tag",
            },
        ]
        assert _build_tag_coedges(relationships) == []


# ── knowledge_graph end-to-end via the FastMCP harness ──────────────


@pytest.fixture
def graph_fixture(tmp_path):
    """Build a RelationshipGraph from a small fixture vault."""
    # Two files in the same scope, one shared tag, one wikilink between
    # them — exercises both link and tag edges in one go.
    _make_md(
        tmp_path,
        "work",
        "alpha",
        "Refers to work/beta.md and tagged #ml\n",
    )
    _make_md(tmp_path, "work", "beta", "Notes #ml\n")

    g = RelationshipGraph()
    g.build(tmp_path)
    return tmp_path, g


class TestKnowledgeGraphTool:
    def test_returns_valid_json_with_nodes_and_edges(self, graph_fixture):
        knowledge_dir, g = graph_fixture
        out = compute_graph(
            knowledge_dir, g, embedding_service=None
        )
        data = json.loads(out)
        assert "nodes" in data
        assert "edges" in data
        assert "stats" in data

    def test_nodes_include_both_files(self, graph_fixture):
        knowledge_dir, g = graph_fixture
        data = json.loads(
            compute_graph(knowledge_dir, g, embedding_service=None)
        )
        ids = {n["id"] for n in data["nodes"]}
        assert "work/alpha" in ids
        assert "work/beta" in ids

    def test_edges_have_link_and_tag_kinds(self, graph_fixture):
        knowledge_dir, g = graph_fixture
        data = json.loads(
            compute_graph(knowledge_dir, g, embedding_service=None)
        )
        kinds = {e["kind"] for e in data["edges"]}
        assert "tag" in kinds
        # The "work/beta.md" file ref in alpha gives us a link edge too.
        assert "link" in kinds

    def test_include_tags_false_drops_tag_edges(self, graph_fixture):
        knowledge_dir, g = graph_fixture
        data = json.loads(
            compute_graph(
                knowledge_dir, g, embedding_service=None, include_tags=False
            )
        )
        kinds = {e["kind"] for e in data["edges"]}
        assert "tag" not in kinds

    def test_scope_filter_drops_other_scopes(self, tmp_path):
        _make_md(tmp_path, "work", "a", "x\n")
        _make_md(tmp_path, "school", "b", "y\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        data = json.loads(
            compute_graph(
                tmp_path,
                g,
                embedding_service=None,
                allowed_scopes_filter={"work"},
            )
        )
        scopes = {n["scope"] for n in data["nodes"]}
        assert scopes == {"work"}

    def test_node_id_helper(self):
        assert _file_node_id("work", "doc") == "work/doc"
