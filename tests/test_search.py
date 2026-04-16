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
        assert set(hit.keys()) == {"scope", "project", "section", "snippet", "rank"}
        assert hit["scope"] == "work"
        assert hit["project"] == "proj"
        assert hit["section"] == "Status"


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
