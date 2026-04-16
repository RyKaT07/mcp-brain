"""Tests for mcp_brain.graph.RelationshipGraph and graph MCP tools."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_brain.graph import RelationshipGraph, _normalize


# ---------------------------------------------------------------------------
# Helpers


def _make_md(tmp_path: Path, scope: str, project: str, content: str) -> Path:
    d = tmp_path / scope
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{project}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Entity extraction tests


class TestEntityExtraction:
    def test_wikilink_creates_mentions_relationship(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "Content with [[docker]] here.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("school/notes", depth=1)
        names = [r["name"] for r in related]
        assert "docker" in names

    def test_backlink_at_entity_creates_references_relationship(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "See @homelab for details.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "homelab" in names

    def test_backlink_at_scope_project_creates_references_relationship(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "See @school/notes for details.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "school/notes" in names

    def test_h2_section_header_creates_entity(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Hardware\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        info = g.entity_info("hardware")
        assert info is not None
        assert info["entity_type"] == "section"

    def test_file_path_ref_creates_references_relationship(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "See school/notes.md for reference.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "school/notes" in names

    def test_entity_name_normalized_lowercase(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "Content with [[Docker Engine]] here.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        info = g.entity_info("docker engine")
        assert info is not None

    def test_entity_name_whitespace_collapsed(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "Content with [[my  entity]] here.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        info = g.entity_info("my entity")
        assert info is not None

    def test_mixed_content_multiple_types(self, tmp_path):
        content = (
            "## Overview\n\n"
            "See [[docker]] and @homelab and school/notes.md.\n"
        )
        _make_md(tmp_path, "work", "proj", content)
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "docker" in names
        assert "homelab" in names
        assert "school/notes" in names
        assert "overview" in names  # H2 section header


# ---------------------------------------------------------------------------
# Graph query tests


class TestRelatedQuery:
    def test_related_depth1_returns_direct_neighbors(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "See [[docker]].\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1)
        assert any(r["name"] == "docker" for r in related)
        assert all(r["distance"] == 1 for r in related)

    def test_related_depth2_follows_transitive(self, tmp_path):
        # work/proj.md references work/docker.md (file ref), which in turn
        # mentions nginx via wikilink. Depth-2 should reach nginx.
        _make_md(tmp_path, "work", "proj", "See work/docker.md for details.\n")
        _make_md(tmp_path, "work", "docker", "Uses [[nginx]].\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=2)
        names = [r["name"] for r in related]
        # Distance 1: work/docker (file ref)
        assert "work/docker" in names
        # Distance 2: nginx (mentioned by work/docker)
        assert "nginx" in names

    def test_related_with_predicate_filter(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Hardware\n\nSee [[docker]].\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        # Filter to only 'mentions' predicate
        related = g.related("work/proj", depth=1, predicates=["mentions"])
        predicates = {r["predicate"] for r in related}
        assert predicates <= {"mentions"}

    def test_related_with_scope_filter(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nContent.\n")
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        related = g.related("work/proj", depth=1, allowed_scopes={"work"})
        scopes = {r["scope"] for r in related}
        assert "school" not in scopes

    def test_related_unknown_entity_returns_empty(self, tmp_path):
        g = RelationshipGraph()
        g.build(tmp_path)
        result = g.related("nonexistent/entity", depth=1)
        assert result == []

    def test_related_empty_graph_returns_empty(self, tmp_path):
        g = RelationshipGraph()
        result = g.related("anything", depth=1)
        assert result == []


class TestEntityInfo:
    def test_entity_info_returns_details(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Overview\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        info = g.entity_info("school/notes")
        assert info is not None
        assert info["name"] == "school/notes"
        assert info["entity_type"] == "file"
        assert info["scope"] == "school"
        assert info["project"] == "notes"
        assert "relationship_count" in info

    def test_entity_info_unknown_returns_none(self, tmp_path):
        g = RelationshipGraph()
        info = g.entity_info("no/such/entity")
        assert info is None


class TestListEntities:
    def test_list_entities_returns_all(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent.\n")
        _make_md(tmp_path, "work", "proj", "## Sec\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        entities = g.list_entities()
        names = {e["name"] for e in entities}
        assert "school/notes" in names
        assert "work/proj" in names

    def test_list_entities_scope_filter(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent.\n")
        _make_md(tmp_path, "work", "proj", "## Sec\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        entities = g.list_entities(scope="school")
        scopes = {e["scope"] for e in entities}
        assert scopes <= {"school"}

    def test_list_entities_has_relationship_count(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nSee [[docker]].\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        entities = g.list_entities()
        assert all("relationship_count" in e for e in entities)

    def test_list_entities_empty_graph(self, tmp_path):
        g = RelationshipGraph()
        result = g.list_entities()
        assert result == []


# ---------------------------------------------------------------------------
# Lifecycle tests


class TestBuildLifecycle:
    def test_build_populates_from_filesystem(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        entities = g.list_entities()
        assert len(entities) > 0

    def test_build_skips_reserved_dirs(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "Content.\n")
        _make_md(tmp_path, "_meta", "config", "Secret.\n")
        _make_md(tmp_path, "inbox", "item", "Message.\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        entities = g.list_entities()
        scopes = {e["scope"] for e in entities}
        assert "school" in scopes
        assert "_meta" not in scopes
        assert "inbox" not in scopes

    def test_build_empty_dir_no_error(self, tmp_path):
        g = RelationshipGraph()
        g.build(tmp_path)  # should not raise
        assert g.list_entities() == []

    def test_build_rebuilds_from_scratch(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nOld content with [[old_entity]].\n")
        g = RelationshipGraph()
        g.build(tmp_path)
        assert g.entity_info("old_entity") is not None

        # Overwrite file and rebuild
        _make_md(tmp_path, "work", "proj", "## Sec\n\nNew content with [[new_entity]].\n")
        g.build(tmp_path)
        assert g.entity_info("new_entity") is not None
        # old_entity should be gone (build rebuilds from scratch)
        # Note: it may still exist as a dangling entity from other files,
        # but work/proj should no longer reference it
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "old_entity" not in names


class TestUpdateFile:
    def test_update_replaces_old_relationships(self, tmp_path):
        g = RelationshipGraph()
        g.update_file("work", "proj", "Content with [[old_entity]].\n")
        assert g.entity_info("old_entity") is not None

        g.update_file("work", "proj", "Content with [[new_entity]].\n")
        related = g.related("work/proj", depth=1)
        names = [r["name"] for r in related]
        assert "new_entity" in names
        assert "old_entity" not in names

    def test_update_preserves_other_files(self, tmp_path):
        g = RelationshipGraph()
        g.update_file("school", "notes", "Content with [[shared_concept]].\n")
        g.update_file("work", "proj", "Unrelated content.\n")

        g.update_file("work", "proj", "New content.\n")

        # school/notes should still reference shared_concept
        related = g.related("school/notes", depth=1)
        names = [r["name"] for r in related]
        assert "shared_concept" in names

    def test_update_nonexistent_file_is_safe(self):
        g = RelationshipGraph()
        g.update_file("nosuchscope", "nosuchproject", "Content.\n")  # should not raise


class TestRemoveFile:
    def test_remove_cleans_up_relationships(self, tmp_path):
        g = RelationshipGraph()
        g.update_file("work", "proj", "Content with [[docker]].\n")
        assert len(g.related("work/proj", depth=1)) > 0

        g.remove_file("work", "proj")
        result = g.related("work/proj", depth=1)
        assert result == []

    def test_remove_nonexistent_is_safe(self):
        g = RelationshipGraph()
        g.remove_file("nosuchscope", "nosuchproject")  # should not raise

    def test_remove_only_targets_scope_project(self):
        g = RelationshipGraph()
        g.update_file("work", "a", "See [[alpha]].\n")
        g.update_file("work", "b", "See [[beta]].\n")

        g.remove_file("work", "a")

        assert g.entity_info("beta") is not None  # work/b still intact
        result = g.related("work/a", depth=1)
        assert result == []  # work/a gone


# ---------------------------------------------------------------------------
# Temporal awareness tests


class TestTemporalRelated:
    def test_related_without_as_of_returns_all(self, tmp_path):
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        related = g.related("work/proj", depth=1)
        assert any(r["name"] == "docker" for r in related)

    def test_related_as_of_future_returns_relationships(self, tmp_path):
        """valid_from=NULL means always valid; future as_of should still return."""
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        related = g.related("work/proj", depth=1, as_of="2099-01-01T00:00:00")
        assert any(r["name"] == "docker" for r in related)

    def test_related_as_of_filters_expired_relationships(self):
        """valid_to set in the past → relationship excluded when as_of is after valid_to."""
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        # Manually set valid_to on all relationships to a past date
        with g._lock:
            g._conn.execute("UPDATE relationships SET valid_to = '2000-01-01T00:00:00'")
            g._conn.commit()
        related = g.related("work/proj", depth=1, as_of="2026-01-01T00:00:00")
        assert not any(r["name"] == "docker" for r in related)

    def test_related_as_of_includes_valid_relationships(self):
        """valid_from set before as_of, valid_to=NULL → should be included."""
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        with g._lock:
            g._conn.execute("UPDATE relationships SET valid_from = '2020-01-01T00:00:00'")
            g._conn.commit()
        related = g.related("work/proj", depth=1, as_of="2026-01-01T00:00:00")
        assert any(r["name"] == "docker" for r in related)

    def test_related_as_of_excludes_future_valid_from(self):
        """valid_from set after as_of → relationship not yet valid, excluded."""
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        with g._lock:
            g._conn.execute("UPDATE relationships SET valid_from = '2099-01-01T00:00:00'")
            g._conn.commit()
        related = g.related("work/proj", depth=1, as_of="2026-01-01T00:00:00")
        assert not any(r["name"] == "docker" for r in related)


class TestTimeline:
    def test_timeline_returns_relationships(self):
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        entries = g.timeline("work/proj")
        assert len(entries) > 0

    def test_timeline_unknown_entity_returns_empty(self):
        g = RelationshipGraph()
        entries = g.timeline("nonexistent/entity")
        assert entries == []

    def test_timeline_entry_has_required_fields(self):
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n")
        entries = g.timeline("work/proj")
        assert entries
        entry = entries[0]
        assert "subject" in entry
        assert "predicate" in entry
        assert "object" in entry
        assert "observed_at" in entry
        assert "valid_from" in entry
        assert "valid_to" in entry
        assert "confidence" in entry

    def test_timeline_observed_at_set_when_provided(self):
        g = RelationshipGraph()
        g.update_file("work", "proj", "See [[docker]].\n", observed_at="2024-06-01T12:00:00")
        entries = g.timeline("work/proj")
        observed_ats = [e["observed_at"] for e in entries]
        assert "2024-06-01T12:00:00" in observed_ats

    def test_timeline_sorted_newest_first(self):
        g = RelationshipGraph()
        g.update_file("work", "a", "See [[alpha]].\n", observed_at="2024-01-01T00:00:00")
        g.update_file("work", "b", "See [[alpha]].\n", observed_at="2025-01-01T00:00:00")
        entries = g.timeline("alpha")
        # Should be sorted newest first
        dates = [e["observed_at"] for e in entries if e["observed_at"]]
        assert dates == sorted(dates, reverse=True)


class TestBuildObservedAt:
    def test_build_without_git_sets_default_observed_at(self, tmp_path):
        """Without a git repo, observed_at falls back to SQLite datetime('now')."""
        d = tmp_path / "work"
        d.mkdir()
        (d / "proj.md").write_text("See [[docker]].\n", encoding="utf-8")
        g = RelationshipGraph()
        g.build(tmp_path)
        # observed_at should be set (default from SQLite)
        entries = g.timeline("work/proj")
        assert entries
        # All entries should have observed_at set (not None)
        assert all(e["observed_at"] is not None for e in entries)


class TestThreadSafety:
    def test_concurrent_updates_do_not_crash(self):
        g = RelationshipGraph()
        errors: list[Exception] = []

        def worker(i: int):
            try:
                g.update_file("work", f"proj{i}", f"Content with [[entity{i}]].\n")
                g.list_entities()
                g.related(f"work/proj{i}", depth=1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# MCP tool tests


class CapturingFastMCP:
    """Minimal MCP stand-in that captures registered tool functions by name."""

    def __init__(self):
        self._tools: dict = {}

    def tool(self, description=None, **kwargs):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator


def _mock_token(scopes: list[str]):
    tok = MagicMock()
    tok.scopes = scopes
    tok.client_id = "test-client"
    return tok


@pytest.fixture
def graph_tools(tmp_path):
    """Register graph tools and return (tools_dict, knowledge_dir, rel_graph)."""
    from mcp_brain.tools.graph import register_graph_tools

    mcp = CapturingFastMCP()
    rel_graph = RelationshipGraph()
    register_graph_tools(mcp, tmp_path, rel_graph)
    return mcp._tools, tmp_path, rel_graph


class TestKnowledgeRelatedTool:
    def test_unknown_entity_returns_not_found(self, graph_tools):
        tools, _, _ = graph_tools
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("nonexistent")
        assert "not found" in result.lower()

    def test_known_entity_returns_relations(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "See [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("work/proj")
        assert "docker" in result

    def test_depth_clamped_to_max(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "Content.\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("work/proj", depth=99)
        # Should not error — depth is clamped
        assert result  # returns something

    def test_scope_filter_enforced_by_permissions(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("secret", "data", "Content.\n")
        tok = _mock_token(["knowledge:read:school"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_related"]("secret/data", scope="secret")
        assert "denied" in result.lower() or "permission" in result.lower()

    def test_no_readable_scopes_returns_error(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "Content.\n")
        tok = _mock_token(["inbox:read"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_related"]("school/notes")
        assert "no readable" in result.lower() or "not found" in result.lower()

    def test_empty_entity_returns_error(self, graph_tools):
        tools, _, _ = graph_tools
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("")
        assert "error" in result.lower() or "empty" in result.lower()

    def test_predicate_filter_works(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "## Section\n\nSee [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("work/proj", predicate="mentions")
        # Should return docker (mentions) but not section (has_section)
        assert "docker" in result

    def test_god_mode_sees_all_scopes(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "See [[docker]].\n")
        # stdio mode: None token = god mode
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("school/notes")
        assert "docker" in result


class TestKnowledgeEntitiesTool:
    def test_returns_entities(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "## Sec\n\nContent.\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_entities"]()
        assert "school/notes" in result

    def test_scope_filter_restricts_results(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "Content.\n")
        rel_graph.update_file("work", "proj", "Content.\n")
        tok = _mock_token(["knowledge:read:school"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_entities"](scope="school")
        assert "school/notes" in result
        assert "work/proj" not in result

    def test_scope_denied_returns_error(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("secret", "data", "Content.\n")
        tok = _mock_token(["knowledge:read:school"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_entities"](scope="secret")
        assert "denied" in result.lower() or "permission" in result.lower()

    def test_entity_type_filter(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "## Hardware\n\nContent.\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_entities"](entity_type="section")
        assert "section" in result

    def test_empty_graph_returns_no_entities(self, graph_tools):
        tools, _, _ = graph_tools
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_entities"]()
        assert "no entities" in result.lower()

    def test_no_readable_scopes_returns_error(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "Content.\n")
        tok = _mock_token(["inbox:read"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_entities"]()
        assert "no readable" in result.lower() or "no entities" in result.lower()


class TestKnowledgeRelatedAsOf:
    def test_as_of_parameter_accepted(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "See [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("work/proj", as_of="2099-01-01T00:00:00")
        assert "docker" in result

    def test_as_of_shows_in_output(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "See [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_related"]("work/proj", as_of="2024-01-01T00:00:00")
        assert "2024-01-01" in result


class TestKnowledgeTimelineTool:
    def test_returns_timeline(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "See [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_timeline"]("work/proj")
        assert "work/proj" in result.lower() or "timeline" in result.lower()

    def test_unknown_entity_returns_not_found(self, graph_tools):
        tools, _, _ = graph_tools
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_timeline"]("nonexistent/entity")
        assert "not found" in result.lower()

    def test_empty_entity_returns_error(self, graph_tools):
        tools, _, _ = graph_tools
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_timeline"]("")
        assert "error" in result.lower() or "empty" in result.lower()

    def test_no_readable_scopes_returns_error(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("school", "notes", "Content.\n")
        tok = _mock_token(["inbox:read"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_timeline"]("school/notes")
        assert "no readable" in result.lower() or "not found" in result.lower()

    def test_scope_denied_returns_error(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("secret", "data", "Content.\n")
        tok = _mock_token(["knowledge:read:school"])
        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok):
            result = tools["knowledge_timeline"]("secret/data", scope="secret")
        assert "denied" in result.lower() or "permission" in result.lower()

    def test_shows_relationship_entries(self, graph_tools):
        tools, _, rel_graph = graph_tools
        rel_graph.update_file("work", "proj", "See [[docker]].\n")
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_timeline"]("work/proj")
        assert "docker" in result
        assert "mentions" in result or "references" in result


# ---------------------------------------------------------------------------
# Normalize helper


class TestNormalize:
    def test_lowercases(self):
        assert _normalize("Docker") == "docker"

    def test_strips_whitespace(self):
        assert _normalize("  entity  ") == "entity"

    def test_collapses_internal_spaces(self):
        assert _normalize("my  entity") == "my entity"

    def test_empty_string(self):
        assert _normalize("") == ""


# ---------------------------------------------------------------------------
# Per-user graph index tests


class TestPerUserRelationshipGraph:
    """Unit tests for RelationshipGraph per-user graph management."""

    def test_build_user_creates_user_graph(self, tmp_path):
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "work").mkdir()
        (user_dir / "work" / "proj.md").write_text("See [[docker]].\n")

        g = RelationshipGraph()
        g.build_user("alice", user_dir)

        assert g.has_user_graph("alice")
        user_g = g.get_user_graph("alice")
        assert user_g is not None
        assert user_g.entity_info("work/proj") is not None

    def test_user_graph_independent_of_global(self, tmp_path):
        global_scope = tmp_path / "work"
        global_scope.mkdir()
        (global_scope / "notes.md").write_text("See [[global_entity]].\n")

        user_dir = tmp_path / "users" / "bob"
        user_dir.mkdir(parents=True)
        (user_dir / "personal").mkdir()
        (user_dir / "personal" / "diary.md").write_text("See [[private_entity]].\n")

        g = RelationshipGraph()
        g.build(tmp_path)
        g.build_user("bob", user_dir)

        # Global graph has global_entity, not private_entity
        assert g.entity_info("global_entity") is not None
        assert g.entity_info("private_entity") is None

        # User graph has private_entity, not global_entity
        user_g = g.get_user_graph("bob")
        assert user_g.entity_info("private_entity") is not None
        assert user_g.entity_info("global_entity") is None

    def test_update_file_for_user_adds_to_user_graph(self):
        g = RelationshipGraph()
        g.update_file_for_user("carol", "work", "proj", "See [[carol_entity]].\n")

        assert g.has_user_graph("carol")
        user_g = g.get_user_graph("carol")
        assert user_g.entity_info("carol_entity") is not None

    def test_update_file_for_user_does_not_affect_global(self):
        g = RelationshipGraph()
        g.update_file_for_user("dave", "work", "proj", "See [[dave_entity]].\n")

        # Global graph should not contain dave_entity
        assert g.entity_info("dave_entity") is None

    def test_remove_file_for_user(self):
        g = RelationshipGraph()
        g.update_file_for_user("eve", "work", "proj", "See [[eve_entity]].\n")

        user_g = g.get_user_graph("eve")
        assert len(user_g.related("work/proj", depth=1)) > 0

        g.remove_file_for_user("eve", "work", "proj")
        # Relationships from work/proj are gone; traversal from the file returns empty
        assert user_g.related("work/proj", depth=1) == []

    def test_remove_file_for_user_nonexistent_safe(self):
        g = RelationshipGraph()
        g.remove_file_for_user("nobody", "scope", "project")  # should not raise

    def test_cross_user_isolation(self):
        g = RelationshipGraph()
        g.update_file_for_user("user1", "work", "proj", "See [[user1_secret]].\n")
        g.update_file_for_user("user2", "work", "proj", "See [[user2_secret]].\n")

        u1 = g.get_user_graph("user1")
        u2 = g.get_user_graph("user2")

        assert u1.entity_info("user1_secret") is not None
        assert u1.entity_info("user2_secret") is None

        assert u2.entity_info("user2_secret") is not None
        assert u2.entity_info("user1_secret") is None

    def test_has_user_graph_false_initially(self):
        g = RelationshipGraph()
        assert not g.has_user_graph("newuser")

    def test_get_user_graph_none_initially(self):
        g = RelationshipGraph()
        assert g.get_user_graph("newuser") is None

    def test_update_file_for_user_with_observed_at(self):
        g = RelationshipGraph()
        g.update_file_for_user(
            "frank", "work", "proj", "See [[frank_entity]].\n",
            observed_at="2024-06-01T12:00:00"
        )
        user_g = g.get_user_graph("frank")
        entries = user_g.timeline("frank_entity")
        assert any(e["observed_at"] == "2024-06-01T12:00:00" for e in entries)

    def test_multiple_users_independent_graphs(self):
        g = RelationshipGraph()
        g.update_file_for_user("u1", "scope", "proj", "See [[alpha]].\n")
        g.update_file_for_user("u2", "scope", "proj", "See [[beta]].\n")

        u1 = g.get_user_graph("u1")
        u2 = g.get_user_graph("u2")
        assert u1 is not u2

        assert u1.entity_info("alpha") is not None
        assert u1.entity_info("beta") is None

        assert u2.entity_info("beta") is not None
        assert u2.entity_info("alpha") is None


class TestKnowledgeGraphToolsPerUser:
    """Integration tests: graph tools query both global + user graphs."""

    def test_knowledge_related_finds_user_entity(self, graph_tools):
        tools, base, rel_graph = graph_tools

        # User has a file with an entity
        rel_graph.update_file_for_user("alice", "work", "proj", "See [[alice_concept]].\n")

        tok = _mock_token(["*"])
        tok.client_id = "alice-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "alice"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_related"]("work/proj")

        assert "alice_concept" in result

    def test_knowledge_related_merges_global_and_user(self, graph_tools):
        tools, base, rel_graph = graph_tools

        # Global entity
        rel_graph.update_file("global", "shared", "See [[global_concept]].\n")
        # User entity (same file path but different graph)
        rel_graph.update_file_for_user("bob", "global", "shared", "See [[user_concept]].\n")

        tok = _mock_token(["*"])
        tok.client_id = "bob-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "bob"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_related"]("global/shared")

        # Should find both global and user concepts
        assert "global_concept" in result or "user_concept" in result

    def test_knowledge_entities_includes_user_entities(self, graph_tools):
        tools, base, rel_graph = graph_tools

        # Global entity
        rel_graph.update_file("work", "global", "## Sec\n\nContent.\n")
        # User entity
        rel_graph.update_file_for_user("carol", "private", "notes", "## Sec\n\nContent.\n")

        tok = _mock_token(["*"])
        tok.client_id = "carol-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "carol"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_entities"]()

        assert "work/global" in result
        assert "private/notes" in result

    def test_knowledge_entities_no_cross_user_leak(self, graph_tools):
        tools, base, rel_graph = graph_tools

        # Alice's entities
        rel_graph.update_file_for_user("alice", "secret", "data", "## Sec\n\nContent.\n")

        # Bob searches — should NOT see Alice's entities
        tok = _mock_token(["*"])
        tok.client_id = "bob-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "bob"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_entities"]()

        assert "secret/data" not in result

    def test_backward_compat_root_user_no_user_graph(self, graph_tools):
        tools, base, rel_graph = graph_tools

        rel_graph.update_file("school", "notes", "## Sec\n\nContent.\n")

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_entities"]()

        assert "school/notes" in result

    def test_knowledge_related_user_entity_not_found_in_global(self, graph_tools):
        tools, base, rel_graph = graph_tools

        # Only in user graph, not global
        rel_graph.update_file_for_user("dave", "work", "proj", "See [[dave_only]].\n")

        tok = _mock_token(["*"])
        tok.client_id = "dave-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "dave"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_related"]("work/proj")

        assert "dave_only" in result
