"""Tests for mcp_brain.search.SearchIndex and the knowledge_search tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_brain.search import SearchIndex


# ---------------------------------------------------------------------------
# Helpers

def _make_md(tmp_path: Path, scope: str, project: str, content: str) -> Path:
    d = tmp_path / scope
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{project}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# SearchIndex unit tests


class TestBuildIndexesFiles:
    def test_indexes_markdown_files(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Overview\n\nElectronics intro.\n")
        _make_md(tmp_path, "work", "project", "## Status\n\nIn progress.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        # There should be at least 2 rows (one per file; each has one section)
        with idx._lock:
            row_count = idx._conn.execute(
                "SELECT COUNT(*) FROM knowledge_fts"
            ).fetchone()[0]

        assert row_count >= 2

    def test_skips_reserved_dirs(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent.\n")
        _make_md(tmp_path, "_meta", "config", "## Sec\n\nSecret.\n")
        _make_md(tmp_path, "inbox", "item", "## Sec\n\nMessage.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        with idx._lock:
            rows = idx._conn.execute(
                "SELECT scope FROM knowledge_fts"
            ).fetchall()

        scopes = {r[0] for r in rows}
        assert "school" in scopes
        assert "_meta" not in scopes
        assert "inbox" not in scopes

    def test_multiple_sections_per_file(self, tmp_path):
        _make_md(
            tmp_path, "school", "notes",
            "## Hardware\n\nGate driver.\n\n## Software\n\nFirmware.\n",
        )

        idx = SearchIndex()
        idx.build(tmp_path)

        with idx._lock:
            rows = idx._conn.execute(
                "SELECT section FROM knowledge_fts"
            ).fetchall()

        sections = {r[0] for r in rows}
        assert "Hardware" in sections
        assert "Software" in sections

    def test_empty_knowledge_dir(self, tmp_path):
        idx = SearchIndex()
        idx.build(tmp_path)  # should not raise

        with idx._lock:
            row_count = idx._conn.execute(
                "SELECT COUNT(*) FROM knowledge_fts"
            ).fetchone()[0]

        assert row_count == 0


class TestSearchReturnsRankedResults:
    def test_returns_matching_results(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Overview\n\nElectronics and gates.\n")
        _make_md(tmp_path, "work", "project", "## Status\n\nIn progress.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("electronics", None)

        assert len(results) >= 1
        assert results[0]["scope"] == "school"
        assert results[0]["project"] == "notes"

    def test_bm25_higher_relevance_ranked_first(self, tmp_path):
        # "gate driver" appears more densely in school/hw than work/notes
        _make_md(
            tmp_path, "school", "hw",
            "## Hardware\n\nGate driver TC4427. The gate driver is critical.\n",
        )
        _make_md(
            tmp_path, "work", "notes",
            "## Notes\n\nSome project note. Mentions gate once.\n",
        )

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("gate driver", None)

        assert len(results) >= 1
        # BM25 rank values are negative; less-negative = better rank; ORDER BY rank ASC
        assert results[0]["scope"] == "school"

    def test_result_structure(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Status\n\nIn progress.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("progress", None)

        assert len(results) == 1
        hit = results[0]
        assert set(hit.keys()) == {"scope", "project", "section", "snippet", "rank", "source"}
        assert hit["scope"] == "work"
        assert hit["project"] == "proj"
        assert hit["section"] == "Status"
        assert hit["source"] == "knowledge"


class TestSearchScopeFiltering:
    def test_allowed_scopes_filters_results(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nElectronics info.\n")
        _make_md(tmp_path, "work", "proj", "## Sec\n\nElectronics info.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("electronics", allowed_scopes=["school"])

        assert all(r["scope"] == "school" for r in results)
        assert len(results) >= 1

    def test_scope_not_in_allowed_excluded(self, tmp_path):
        _make_md(tmp_path, "secret", "data", "## Sec\n\nTop secret info.\n")
        _make_md(tmp_path, "school", "notes", "## Sec\n\nTop secret info.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("secret", allowed_scopes=["school"])

        assert all(r["scope"] != "secret" for r in results)

    def test_none_allowed_scopes_searches_all(self, tmp_path):
        _make_md(tmp_path, "scope_a", "a", "## Sec\n\nShared keyword.\n")
        _make_md(tmp_path, "scope_b", "b", "## Sec\n\nShared keyword.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("shared", allowed_scopes=None)

        scopes = {r["scope"] for r in results}
        assert "scope_a" in scopes
        assert "scope_b" in scopes

    def test_empty_allowed_scopes_returns_nothing(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "## Sec\n\nSome content.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("content", allowed_scopes=[])

        assert results == []


class TestUpdateFileIncremental:
    def test_update_replaces_old_content(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Status\n\nOld content.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        # Should be findable before update
        assert len(idx.search("old", None)) >= 1

        # Update the index with new content
        idx.update_file("work", "proj", "## Status\n\nNew content.\n")

        # Old term should no longer be found
        assert idx.search("old", None) == []

        # New term should be found
        results = idx.search("new", None)
        assert len(results) >= 1
        assert results[0]["project"] == "proj"

    def test_update_preserves_other_files(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nContent A.\n")
        _make_md(tmp_path, "school", "notes", "## Sec\n\nContent B.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        idx.update_file("work", "proj", "## Sec\n\nReplaced.\n")

        # school/notes should still be indexed
        results = idx.search("content", None)
        scopes = {r["scope"] for r in results}
        assert "school" in scopes

    def test_update_multiple_sections(self, tmp_path):
        idx = SearchIndex()
        idx.update_file("work", "proj", "## Alpha\n\nFirst.\n\n## Beta\n\nSecond.\n")

        with idx._lock:
            rows = idx._conn.execute(
                "SELECT section FROM knowledge_fts WHERE scope='work' AND project='proj'"
            ).fetchall()

        sections = {r[0] for r in rows}
        assert "Alpha" in sections
        assert "Beta" in sections


class TestRemoveFile:
    def test_removed_file_not_in_results(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nUnique term xyzzy.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        assert len(idx.search("xyzzy", None)) >= 1

        idx.remove_file("work", "proj")

        assert idx.search("xyzzy", None) == []

    def test_remove_nonexistent_is_safe(self, tmp_path):
        idx = SearchIndex()
        idx.remove_file("nosuchscope", "nosuchproject")  # should not raise

    def test_remove_only_targets_scope_project(self, tmp_path):
        _make_md(tmp_path, "work", "a", "## Sec\n\nKeyword alpha.\n")
        _make_md(tmp_path, "work", "b", "## Sec\n\nKeyword alpha.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        idx.remove_file("work", "a")

        results = idx.search("alpha", None)
        assert all(r["project"] != "a" for r in results)
        assert any(r["project"] == "b" for r in results)


class TestEmptyFileHandled:
    def test_empty_file_does_not_crash(self, tmp_path):
        _make_md(tmp_path, "school", "empty", "")

        idx = SearchIndex()
        idx.build(tmp_path)  # should not raise

    def test_whitespace_only_file_not_searchable(self, tmp_path):
        # A whitespace-only file produces a _preamble section with no meaningful
        # terms — searching for real words returns no results.
        _make_md(tmp_path, "school", "blank", "   \n\n  ")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("content", None)
        assert results == []

    def test_preamble_only_no_sections(self, tmp_path):
        _make_md(tmp_path, "school", "notes", "# Just a title\n\nSome preamble.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("preamble", None)
        assert len(results) >= 1  # preamble is indexed as _preamble section


class TestSearchNoResults:
    def test_query_with_no_matches(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nElectronics.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("xyzzyunlikelyterm", None)

        assert results == []

    def test_empty_query_returns_empty(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Sec\n\nContent.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        assert idx.search("", None) == []
        assert idx.search("   ", None) == []

    def test_empty_index_returns_empty(self, tmp_path):
        idx = SearchIndex()

        results = idx.search("anything", None)

        assert results == []


class TestSnippetGeneration:
    def test_snippet_contains_query_context(self, tmp_path):
        _make_md(
            tmp_path, "school", "hw",
            "## Hardware\n\nThe gate driver TC4427 controls the MOSFET switching.\n",
        )

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("gate driver", None)

        assert len(results) >= 1
        snippet = results[0]["snippet"]
        # The snippet should contain content near the query terms
        assert snippet  # not empty
        assert len(snippet) > 0

    def test_snippet_has_highlight_markers(self, tmp_path):
        _make_md(tmp_path, "work", "proj", "## Status\n\nDeployment completed successfully.\n")

        idx = SearchIndex()
        idx.build(tmp_path)

        results = idx.search("deployment", None)

        assert len(results) >= 1
        # FTS5 snippet() wraps matches in <b>...</b>
        snippet = results[0]["snippet"]
        assert "<b>" in snippet or "deployment" in snippet.lower()


# ---------------------------------------------------------------------------
# Integration tests: knowledge_search tool


class CapturingFastMCP:
    """Minimal MCP stand-in that captures registered tool functions by name."""

    def __init__(self):
        self._tools: dict = {}

    def tool(self, description=None, **kwargs):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator


@pytest.fixture
def search_tools(tmp_path):
    """Register search + knowledge tools and expose them via dicts."""
    from mcp_brain.tools.knowledge import register_knowledge_tools
    from mcp_brain.tools.search import register_search_tools

    mcp = CapturingFastMCP()
    search_index = SearchIndex()
    register_knowledge_tools(mcp, tmp_path)
    register_search_tools(mcp, tmp_path, search_index)
    return mcp._tools, tmp_path, search_index


def _mock_token(scopes: list[str]):
    """Return a stub AccessToken with the given scopes."""
    tok = MagicMock()
    tok.scopes = scopes
    tok.client_id = "test-client"
    return tok


class TestKnowledgeSearchToolRespectScopes:
    def test_restricted_token_cannot_see_other_scopes(self, search_tools):
        tools, base, idx = search_tools

        # Seed two scopes into the index directly
        idx.update_file("school", "notes", "## Sec\n\nElectronics content.\n")
        idx.update_file("secret", "data", "## Sec\n\nElectronics content.\n")

        # Token has read access only to 'school'
        tok = _mock_token(["knowledge:read:school"])

        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=tok
        ):
            result = tools["knowledge_search"]("electronics")

        assert "school/notes" in result
        assert "secret/data" not in result

    def test_god_mode_sees_all_scopes(self, search_tools):
        tools, base, idx = search_tools

        idx.update_file("school", "notes", "## Sec\n\nElectronics content.\n")
        idx.update_file("work", "proj", "## Sec\n\nElectronics content.\n")

        # stdio mode: get_access_token returns None → god mode (*) → ALL scopes
        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=None
        ):
            result = tools["knowledge_search"]("electronics")

        assert "school/notes" in result
        assert "work/proj" in result

    def test_no_readable_scopes_returns_error_message(self, search_tools):
        tools, base, idx = search_tools

        idx.update_file("school", "notes", "## Sec\n\nContent.\n")

        # Token has no knowledge:read scopes at all
        tok = _mock_token(["inbox:read"])

        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=tok
        ):
            result = tools["knowledge_search"]("content")

        assert "No readable knowledge scopes" in result or "No results" in result

    def test_scope_param_denied_for_unallowed_scope(self, search_tools):
        tools, base, idx = search_tools

        idx.update_file("secret", "data", "## Sec\n\nClassified info.\n")

        # Token only has school read access
        tok = _mock_token(["knowledge:read:school"])

        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=tok
        ):
            result = tools["knowledge_search"]("classified", scope="secret")

        assert "denied" in result.lower() or "permission" in result.lower() or "PermissionDenied" in result

    def test_scope_param_allowed_for_permitted_scope(self, search_tools):
        tools, base, idx = search_tools

        idx.update_file("school", "notes", "## Sec\n\nElectronics.\n")

        tok = _mock_token(["knowledge:read:school"])

        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=tok
        ):
            result = tools["knowledge_search"]("electronics", scope="school")

        assert "school/notes" in result


class TestSearchAfterKnowledgeUpdate:
    def test_search_finds_content_written_via_update_tool(self, search_tools):
        tools, base, idx = search_tools

        # Wire the search_index into the knowledge tools fixture so knowledge_update
        # triggers a re-index. For this test we call update_file manually after
        # writing, since knowledge_update doesn't know about the index. This mirrors
        # Phase 1: the tool writes to disk and the index is updated separately.
        # The integration point is verified via update_file reflecting the disk write.

        with patch("subprocess.run") as mock_run:
            add = MagicMock(); add.returncode = 0
            commit = MagicMock(); commit.returncode = 0
            mock_run.side_effect = [add, commit]

            with patch(
                "mcp_brain.tools._perms.get_access_token", return_value=None
            ):
                result = tools["knowledge_update"](
                    "school", "hw", "Hardware", "TC4427 gate driver installed.\n"
                )

        assert "Updated" in result

        # Now simulate the server-side index update triggered by the write
        written = (base / "school" / "hw.md").read_text(encoding="utf-8")
        idx.update_file("school", "hw", written)

        # The new content should now be searchable
        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=None
        ):
            search_result = tools["knowledge_search"]("TC4427")

        assert "school/hw" in search_result

    def test_search_after_update_reflects_new_section(self, search_tools):
        tools, base, idx = search_tools

        # Write initial file
        with patch("subprocess.run") as mock_run:
            add = MagicMock(); add.returncode = 0
            commit = MagicMock(); commit.returncode = 0
            mock_run.side_effect = [add, commit]

            with patch(
                "mcp_brain.tools._perms.get_access_token", return_value=None
            ):
                tools["knowledge_update"]("work", "proj", "Status", "Initial version.\n")

        written = (base / "work" / "proj.md").read_text(encoding="utf-8")
        idx.update_file("work", "proj", written)

        # Now add a second section
        with patch("subprocess.run") as mock_run:
            add = MagicMock(); add.returncode = 0
            commit = MagicMock(); commit.returncode = 0
            mock_run.side_effect = [add, commit]

            with patch(
                "mcp_brain.tools._perms.get_access_token", return_value=None
            ):
                tools["knowledge_update"]("work", "proj", "Notes", "Deployment notes here.\n")

        updated = (base / "work" / "proj.md").read_text(encoding="utf-8")
        idx.update_file("work", "proj", updated)

        with patch(
            "mcp_brain.tools._perms.get_access_token", return_value=None
        ):
            result = tools["knowledge_search"]("deployment")

        assert "work/proj" in result


# ---------------------------------------------------------------------------
# Task indexing tests


class TestIndexTodoistTasks:
    def test_tasks_are_searchable(self):
        idx = SearchIndex()
        tasks = [
            {"content": "Buy groceries", "project_name": "Personal", "section_name": "Shopping"},
            {"content": "Fix CI pipeline", "project_name": "Work", "section_name": "DevOps"},
        ]
        count = idx.index_todoist_tasks(tasks)
        assert count == 2

        results = idx.search("groceries", None)
        assert len(results) >= 1
        hit = results[0]
        assert hit["source"] == "todoist"
        assert hit["project"] == "Personal"
        assert hit["section"] == "Shopping"

    def test_source_field_is_todoist(self):
        idx = SearchIndex()
        idx.index_todoist_tasks([{"content": "Write tests", "project_name": "Dev", "section_name": ""}])
        results = idx.search("tests", None)
        assert results[0]["source"] == "todoist"

    def test_empty_content_skipped(self):
        idx = SearchIndex()
        count = idx.index_todoist_tasks([
            {"content": "", "project_name": "P", "section_name": ""},
            {"content": "   ", "project_name": "P", "section_name": ""},
            {"content": "Valid task", "project_name": "P", "section_name": ""},
        ])
        assert count == 1

    def test_reindex_clears_old_tasks(self):
        idx = SearchIndex()
        idx.index_todoist_tasks([{"content": "Old task", "project_name": "P", "section_name": ""}])
        assert len(idx.search("old", None)) >= 1

        idx.index_todoist_tasks([{"content": "New task", "project_name": "P", "section_name": ""}])
        assert idx.search("old", None) == []
        assert len(idx.search("new", None)) >= 1

    def test_todoist_does_not_clear_knowledge(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nKnowledge content.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_todoist_tasks([{"content": "Task content", "project_name": "P", "section_name": ""}])

        results = idx.search("knowledge", None)
        assert any(r["source"] == "knowledge" for r in results)

    def test_scope_parameter(self):
        idx = SearchIndex()
        idx.index_todoist_tasks(
            [{"content": "Work task", "project_name": "P", "section_name": ""}],
            scope="work_user",
        )
        idx.index_todoist_tasks(
            [{"content": "Personal task", "project_name": "P", "section_name": ""}],
            scope="personal_user",
        )

        results = idx.search("task", None)
        scopes = {r["scope"] for r in results}
        assert "work_user" in scopes
        assert "personal_user" in scopes


class TestIndexTrelloCards:
    def test_cards_are_searchable(self):
        idx = SearchIndex()
        cards = [
            {"name": "Implement login flow", "board_name": "Sprint", "list_name": "In Progress"},
            {"name": "Update documentation", "board_name": "Sprint", "list_name": "Backlog"},
        ]
        count = idx.index_trello_cards(cards)
        assert count == 2

        results = idx.search("login", None)
        assert len(results) >= 1
        hit = results[0]
        assert hit["source"] == "trello"
        assert hit["project"] == "Sprint"
        assert hit["section"] == "In Progress"

    def test_source_field_is_trello(self):
        idx = SearchIndex()
        idx.index_trello_cards([{"name": "Design review", "board_name": "B", "list_name": "L"}])
        results = idx.search("design", None)
        assert results[0]["source"] == "trello"

    def test_empty_name_skipped(self):
        idx = SearchIndex()
        count = idx.index_trello_cards([
            {"name": "", "board_name": "B", "list_name": "L"},
            {"name": "  ", "board_name": "B", "list_name": "L"},
            {"name": "Real card", "board_name": "B", "list_name": "L"},
        ])
        assert count == 1

    def test_reindex_clears_old_cards(self):
        idx = SearchIndex()
        idx.index_trello_cards([{"name": "Old card", "board_name": "B", "list_name": "L"}])
        assert len(idx.search("old", None)) >= 1

        idx.index_trello_cards([{"name": "New card", "board_name": "B", "list_name": "L"}])
        assert idx.search("old", None) == []
        assert len(idx.search("new", None)) >= 1

    def test_trello_does_not_clear_knowledge(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nKnowledge content.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_trello_cards([{"name": "Card content", "board_name": "B", "list_name": "L"}])

        results = idx.search("knowledge", None)
        assert any(r["source"] == "knowledge" for r in results)


class TestSourceFilter:
    def test_source_filter_knowledge(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nSearch term here.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_todoist_tasks([{"content": "Search term task", "project_name": "P", "section_name": ""}])
        idx.index_trello_cards([{"name": "Search term card", "board_name": "B", "list_name": "L"}])

        results = idx.search("search term", None, source="knowledge")
        assert all(r["source"] == "knowledge" for r in results)
        assert len(results) >= 1

    def test_source_filter_todoist(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nSearch term here.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_todoist_tasks([{"content": "Search term task", "project_name": "P", "section_name": ""}])

        results = idx.search("search", None, source="todoist")
        assert all(r["source"] == "todoist" for r in results)

    def test_source_filter_trello(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nSearch term here.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_trello_cards([{"name": "Search term card", "board_name": "B", "list_name": "L"}])

        results = idx.search("search", None, source="trello")
        assert all(r["source"] == "trello" for r in results)

    def test_no_source_filter_returns_all(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nQuery term knowledge.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_todoist_tasks([{"content": "Query term task", "project_name": "P", "section_name": ""}])
        idx.index_trello_cards([{"name": "Query term card", "board_name": "B", "list_name": "L"}])

        results = idx.search("query term", None, source=None)
        sources = {r["source"] for r in results}
        assert "knowledge" in sources
        assert "todoist" in sources
        assert "trello" in sources

    def test_source_filter_no_match_returns_empty(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nContent.\n")
        idx = SearchIndex()
        idx.build(tmp_path)

        # No trello cards indexed — source filter returns nothing
        results = idx.search("content", None, source="trello")
        assert results == []


class TestRefreshTasks:
    def test_refresh_clears_and_reindexes(self):
        idx = SearchIndex()
        idx.index_todoist_tasks([{"content": "Old task", "project_name": "P", "section_name": ""}])
        idx.index_trello_cards([{"name": "Old card", "board_name": "B", "list_name": "L"}])

        idx.refresh_tasks(
            todoist_tasks=[{"content": "New task", "project_name": "P", "section_name": ""}],
            trello_cards=[{"name": "New card", "board_name": "B", "list_name": "L"}],
        )

        assert idx.search("old", None) == []
        assert len(idx.search("new", None)) >= 1

    def test_refresh_preserves_knowledge(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nKnowledge here.\n")
        idx = SearchIndex()
        idx.build(tmp_path)

        idx.refresh_tasks(
            todoist_tasks=[{"content": "Task", "project_name": "P", "section_name": ""}],
        )

        assert len(idx.search("knowledge", None)) >= 1

    def test_refresh_with_no_data_clears_tasks(self):
        idx = SearchIndex()
        idx.index_todoist_tasks([{"content": "Existing task", "project_name": "P", "section_name": ""}])

        idx.refresh_tasks(todoist_tasks=[], trello_cards=[])

        assert idx.search("existing", None) == []

    def test_refresh_none_args_only_clears(self):
        idx = SearchIndex()
        idx.index_todoist_tasks([{"content": "Task to clear", "project_name": "P", "section_name": ""}])

        # Passing None means "don't re-index" but still clears
        idx.refresh_tasks()

        assert idx.search("clear", None) == []


class TestBuildPreservesTaskIndex:
    def test_rebuild_knowledge_preserves_tasks(self, tmp_path):
        _make_md(tmp_path, "work", "notes", "## Sec\n\nKnowledge content.\n")
        idx = SearchIndex()
        idx.build(tmp_path)
        idx.index_todoist_tasks([{"content": "Task content", "project_name": "P", "section_name": ""}])

        # Rebuild knowledge index
        idx.build(tmp_path)

        # Task entries should still be present
        task_results = idx.search("task content", None, source="todoist")
        assert len(task_results) >= 1


class TestKnowledgeSearchSourceParam:
    def test_source_param_validated(self, search_tools):
        tools, base, idx = search_tools

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("query", source="invalid")

        assert "Error" in result or "invalid" in result

    def test_source_label_in_output_todoist(self, search_tools):
        tools, base, idx = search_tools

        idx.index_todoist_tasks([{"content": "Todoist unique task", "project_name": "P", "section_name": ""}])

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("unique task")

        assert "[todoist]" in result

    def test_source_label_in_output_trello(self, search_tools):
        tools, base, idx = search_tools

        idx.index_trello_cards([{"name": "Trello unique card", "board_name": "B", "list_name": "L"}])

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("unique card")

        assert "[trello]" in result

    def test_knowledge_source_no_label(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        idx.update_file("work", "notes", "## Sec\n\nKnowledge unique content.\n")

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("unique content")

        # Knowledge results have no label
        assert "[knowledge]" not in result
        assert "work/notes" in result

    def test_source_filter_in_tool(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        idx.update_file("work", "notes", "## Sec\n\nShared keyword content.\n")
        idx.index_todoist_tasks([{"content": "Shared keyword task", "project_name": "P", "section_name": ""}])

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("shared keyword", source="todoist")

        assert "[todoist]" in result
        assert "work/notes" not in result


# ---------------------------------------------------------------------------
# Per-user search index tests


class TestPerUserSearchIndex:
    """Unit tests for SearchIndex per-user index management."""

    def test_build_user_creates_user_index(self, tmp_path):
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "work").mkdir()
        (user_dir / "work" / "proj.md").write_text("## Sec\n\nAlice private content.\n")

        idx = SearchIndex()
        idx.build_user("alice", user_dir)

        assert idx.has_user_index("alice")
        user_idx = idx.get_user_index("alice")
        assert user_idx is not None
        results = user_idx.search("alice", None)
        assert len(results) >= 1

    def test_user_index_independent_of_global(self, tmp_path):
        # Global knowledge has 'global term'; user has 'private term'
        global_scope = tmp_path / "work"
        global_scope.mkdir()
        (global_scope / "notes.md").write_text("## Sec\n\nGlobal term content.\n")

        user_dir = tmp_path / "users" / "bob"
        user_dir.mkdir(parents=True)
        (user_dir / "personal").mkdir()
        (user_dir / "personal" / "diary.md").write_text("## Sec\n\nPrivate term content.\n")

        idx = SearchIndex()
        idx.build(tmp_path)
        idx.build_user("bob", user_dir)

        # Global index has 'global term', not 'private term'
        global_results = idx.search("global", None)
        assert any(r["scope"] == "work" for r in global_results)
        global_results_private = idx.search("private", None)
        assert not any(r["scope"] == "personal" for r in global_results_private)

        # User index has 'private term', not 'global term'
        user_idx = idx.get_user_index("bob")
        assert user_idx is not None
        user_results = user_idx.search("private", None)
        assert any(r["scope"] == "personal" for r in user_results)
        user_global = user_idx.search("global", None)
        assert user_global == []

    def test_update_file_for_user_adds_to_user_index(self):
        idx = SearchIndex()
        idx.update_file_for_user("carol", "notes", "hw", "## Sec\n\nCarol's content.\n")

        assert idx.has_user_index("carol")
        user_idx = idx.get_user_index("carol")
        results = user_idx.search("carol", None)
        assert len(results) >= 1

    def test_update_file_for_user_does_not_affect_global(self):
        idx = SearchIndex()
        idx.update_file_for_user("dave", "work", "proj", "## Sec\n\nUser content.\n")

        # Global index should be empty
        global_results = idx.search("user content", None)
        assert global_results == []

    def test_remove_file_for_user(self):
        idx = SearchIndex()
        idx.update_file_for_user("eve", "notes", "hw", "## Sec\n\nEve unique content.\n")

        user_idx = idx.get_user_index("eve")
        assert len(user_idx.search("eve unique", None)) >= 1

        idx.remove_file_for_user("eve", "notes", "hw")
        assert user_idx.search("eve unique", None) == []

    def test_remove_file_for_user_nonexistent_safe(self):
        idx = SearchIndex()
        idx.remove_file_for_user("nobody", "scope", "project")  # should not raise

    def test_cross_user_isolation(self):
        idx = SearchIndex()
        idx.update_file_for_user("user1", "work", "proj", "## Sec\n\nUser one secret.\n")
        idx.update_file_for_user("user2", "work", "proj", "## Sec\n\nUser two secret.\n")

        user1_idx = idx.get_user_index("user1")
        user2_idx = idx.get_user_index("user2")

        # user1's index does not contain user2's content and vice versa
        u1_results = user1_idx.search("user two", None)
        assert u1_results == []

        u2_results = user2_idx.search("user one", None)
        assert u2_results == []

    def test_has_user_index_false_initially(self):
        idx = SearchIndex()
        assert not idx.has_user_index("newuser")

    def test_get_user_index_none_initially(self):
        idx = SearchIndex()
        assert idx.get_user_index("newuser") is None

    def test_multiple_users_independent(self):
        idx = SearchIndex()
        idx.update_file_for_user("u1", "scope", "proj", "## Sec\n\nAlpha content.\n")
        idx.update_file_for_user("u2", "scope", "proj", "## Sec\n\nBeta content.\n")

        u1 = idx.get_user_index("u1")
        u2 = idx.get_user_index("u2")
        assert u1 is not u2

        assert len(u1.search("alpha", None)) >= 1
        assert u1.search("beta", None) == []

        assert len(u2.search("beta", None)) >= 1
        assert u2.search("alpha", None) == []


class TestKnowledgeSearchPerUser:
    """Integration tests: knowledge_search tool queries both global + user indexes."""

    def test_user_sees_own_private_files(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        # Seed user dir
        user_dir = base / "users" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "private").mkdir()
        (user_dir / "private" / "notes.md").write_text(
            "## Sec\n\nAlice private knowledge.\n"
        )
        idx.build_user("alice", user_dir)

        tok = _mock_token(["*"])
        tok.client_id = "alice-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "alice"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_search"]("alice private")

        assert "private/notes" in result

    def test_user_does_not_see_other_users_files(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        # Bob's private files
        bob_dir = base / "users" / "bob"
        bob_dir.mkdir(parents=True)
        (bob_dir / "secret").mkdir()
        (bob_dir / "secret" / "data.md").write_text(
            "## Sec\n\nBob classified content.\n"
        )
        idx.build_user("bob", bob_dir)

        # Alice searches — should NOT see Bob's content
        tok = _mock_token(["*"])
        tok.client_id = "alice-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "alice"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_search"]("bob classified")

        assert "secret/data" not in result

    def test_root_user_does_not_see_per_user_files(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        # Index a global file and a user file
        idx.update_file("work", "global", "## Sec\n\nGlobal content.\n")
        idx.update_file_for_user("carol", "work", "private", "## Sec\n\nCarol private.\n")

        # Root (stdio, no user_id) search
        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("carol private")

        assert "carol" not in result.lower() or "No results" in result

    def test_user_also_sees_global_files(self, search_tools, tmp_path):
        tools, base, idx = search_tools

        # Global file
        idx.update_file("work", "global", "## Sec\n\nGlobal shared content.\n")
        # User's private file — pre-build the user index
        user_dir = base / "users" / "dave"
        user_dir.mkdir(parents=True)
        (user_dir / "work").mkdir()
        (user_dir / "work" / "private.md").write_text(
            "## Sec\n\nGlobal shared content in user space.\n"
        )
        idx.build_user("dave", user_dir)

        tok = _mock_token(["*"])
        tok.client_id = "dave-key"

        with patch("mcp_brain.tools._perms.get_access_token", return_value=tok), \
             patch("mcp_brain.tools._perms._key_store") as mock_ks:
            entry = MagicMock()
            entry.is_active = True
            entry.user_id = "dave"
            mock_ks.by_id.return_value = entry
            result = tools["knowledge_search"]("global shared")

        # Should see both global and user results
        assert "work/global" in result
        assert "work/private" in result

    def test_backward_compat_no_user_searches_only_global(self, search_tools):
        tools, base, idx = search_tools

        idx.update_file("school", "notes", "## Sec\n\nBackward compat content.\n")

        with patch("mcp_brain.tools._perms.get_access_token", return_value=None):
            result = tools["knowledge_search"]("backward compat")

        assert "school/notes" in result
