"""Tests for mcp_brain.tools.knowledge — section parsing, path resolution, validation."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_brain.tools.knowledge import (
    _KNOWLEDGE_UPDATE_BASE_DESCRIPTION,
    _build_knowledge_update_description,
    _git_commit,
    _parse_sections,
    _rebuild_markdown,
    _resolve_file,
    _sanitize,
    _validate_scope_project,
    _validate_scope_writable,
)


# ---------------------------------------------------------------------------
# _sanitize

class TestSanitize:
    def test_alphanumeric_unchanged(self):
        assert _sanitize("school") == "school"
        assert _sanitize("work123") == "work123"

    def test_hyphens_and_underscores_kept(self):
        assert _sanitize("my-project") == "my-project"
        assert _sanitize("my_project") == "my_project"

    def test_spaces_stripped(self):
        assert _sanitize("my project") == "myproject"

    def test_special_chars_stripped(self):
        assert _sanitize("../etc/passwd") == "etcpasswd"
        assert _sanitize("../../escape") == "escape"
        assert _sanitize("file.md") == "filemd"

    def test_empty_string_stays_empty(self):
        assert _sanitize("") == ""

    def test_dots_stripped(self):
        # Dots are not in [a-zA-Z0-9_-], so they are stripped
        assert _sanitize("my.file") == "myfile"


# ---------------------------------------------------------------------------
# _validate_scope_project

class TestValidateScopeProject:
    def test_valid_names_return_none(self):
        assert _validate_scope_project("school", "power-electronics") is None
        assert _validate_scope_project("homelab", "proxmox_setup") is None
        assert _validate_scope_project("work", "onboarding") is None

    def test_empty_scope_returns_error(self):
        err = _validate_scope_project("", "project")
        assert err is not None
        assert "scope" in err.lower() or "Invalid" in err

    def test_empty_project_returns_error(self):
        err = _validate_scope_project("school", "")
        assert err is not None
        assert "project" in err.lower() or "Invalid" in err

    def test_sanitized_to_empty_returns_error(self):
        # Dots and slashes sanitize to empty string
        err = _validate_scope_project("...", "project")
        assert err is not None

    def test_scope_with_only_specials_errors(self):
        err = _validate_scope_project("!@#$", "project")
        assert err is not None


# ---------------------------------------------------------------------------
# _validate_scope_writable

class TestValidateScopeWritable:
    def test_normal_scope_returns_none(self):
        assert _validate_scope_writable("school") is None
        assert _validate_scope_writable("work") is None
        assert _validate_scope_writable("homelab") is None

    def test_underscore_prefixed_scope_returns_error(self):
        err = _validate_scope_writable("_meta")
        assert err is not None
        assert "reserved" in err.lower() or "_meta" in err

    def test_underscore_prefixed_sanitized_error(self):
        # Input that sanitizes to something starting with "_"
        err = _validate_scope_writable("_internal")
        assert err is not None


# ---------------------------------------------------------------------------
# _parse_sections / _rebuild_markdown

class TestParseSections:
    def test_empty_file(self):
        sections = _parse_sections("")
        assert "_preamble" in sections
        assert sections["_preamble"] == ""

    def test_preamble_only(self):
        content = "# My Project\n\nSome intro text.\n"
        sections = _parse_sections(content)
        assert "_preamble" in sections
        assert "My Project" in sections["_preamble"]
        assert len(sections) == 1

    def test_single_h2_section(self):
        content = "## Overview\n\nThis is the overview.\n"
        sections = _parse_sections(content)
        assert "Overview" in sections
        assert "This is the overview." in sections["Overview"]

    def test_multiple_sections(self):
        content = (
            "## Hardware\n\nGate driver: TC4427\n\n"
            "## Software\n\nFirmware version: 1.2\n"
        )
        sections = _parse_sections(content)
        assert "Hardware" in sections
        assert "Software" in sections
        assert "TC4427" in sections["Hardware"]
        assert "Firmware" in sections["Software"]

    def test_preamble_plus_sections(self):
        content = (
            "# Project: Power Electronics\n\n"
            "## Hardware\n\nGate driver: TC4427\n\n"
            "## Notes\n\nSome notes.\n"
        )
        sections = _parse_sections(content)
        assert "_preamble" in sections
        assert "Hardware" in sections
        assert "Notes" in sections

    def test_round_trip(self):
        content = "## Alpha\n\nBody of alpha.\n\n## Beta\n\nBody of beta.\n"
        sections = _parse_sections(content)
        rebuilt = _rebuild_markdown(sections)
        # Both sections should survive the round-trip
        assert "## Alpha" in rebuilt
        assert "Body of alpha." in rebuilt
        assert "## Beta" in rebuilt
        assert "Body of beta." in rebuilt


class TestRebuildMarkdown:
    def test_empty_preamble_omitted(self):
        sections = {"_preamble": "", "Overview": "\nSome text.\n"}
        result = _rebuild_markdown(sections)
        assert "## Overview" in result
        assert "_preamble" not in result

    def test_non_empty_preamble_included(self):
        sections = {"_preamble": "# Title\n", "Overview": "\nSome text.\n"}
        result = _rebuild_markdown(sections)
        assert "# Title" in result
        assert "## Overview" in result


# ---------------------------------------------------------------------------
# _resolve_file

class TestResolveFile:
    def test_basic_resolution(self, tmp_path):
        p = _resolve_file(tmp_path, "school", "power-electronics")
        assert p == tmp_path / "school" / "power-electronics.md"

    def test_sanitization_applied(self, tmp_path):
        # Dots in project name are stripped
        p = _resolve_file(tmp_path, "school", "my.project")
        assert p == tmp_path / "school" / "myproject.md"

    def test_path_traversal_neutralized(self, tmp_path):
        # ".." characters are stripped; cannot escape knowledge_dir
        p = _resolve_file(tmp_path, "..dangerous", "..file")
        # The sanitized path should not be above tmp_path
        assert str(tmp_path) in str(p)
        assert ".." not in str(p)


# ---------------------------------------------------------------------------
# _build_knowledge_update_description

class TestBuildKnowledgeUpdateDescription:
    def test_empty_policy_returns_base(self):
        result = _build_knowledge_update_description("")
        assert result == _KNOWLEDGE_UPDATE_BASE_DESCRIPTION

    def test_whitespace_only_policy_returns_base(self):
        result = _build_knowledge_update_description("   \n  ")
        assert result == _KNOWLEDGE_UPDATE_BASE_DESCRIPTION

    def test_policy_prepended_with_separator(self):
        policy = "Only update on Tuesdays."
        result = _build_knowledge_update_description(policy)
        assert result.startswith(policy)
        assert "---" in result
        assert _KNOWLEDGE_UPDATE_BASE_DESCRIPTION in result

    def test_policy_with_leading_trailing_whitespace_stripped(self):
        policy = "  Be careful.  "
        result = _build_knowledge_update_description(policy)
        assert result.startswith("Be careful.")


# ---------------------------------------------------------------------------
# _git_commit

class TestGitCommit:
    def test_successful_commit(self, tmp_path):
        filepath = tmp_path / "school" / "notes.md"
        filepath.parent.mkdir()
        filepath.touch()

        with patch("subprocess.run") as mock_run:
            mock_add = MagicMock()
            mock_add.returncode = 0
            mock_commit = MagicMock()
            mock_commit.returncode = 0
            mock_run.side_effect = [mock_add, mock_commit]

            _git_commit(tmp_path, filepath, "test: add notes")

        assert mock_run.call_count == 2
        add_cmd = mock_run.call_args_list[0][0][0]
        commit_cmd = mock_run.call_args_list[1][0][0]
        assert add_cmd[0] == "git"
        assert add_cmd[1] == "add"
        assert commit_cmd[0] == "git"
        assert commit_cmd[1] == "commit"

    def test_git_add_failure_skips_commit(self, tmp_path):
        filepath = tmp_path / "notes.md"
        filepath.touch()

        with patch("subprocess.run") as mock_run:
            mock_add = MagicMock()
            mock_add.returncode = 1
            mock_add.stderr = "fatal: not a git repo"
            mock_add.stdout = ""
            mock_run.return_value = mock_add

            _git_commit(tmp_path, filepath, "test: update notes")

        # Only git add should have been called, not git commit
        assert mock_run.call_count == 1

    def test_git_commit_failure_logs_warning(self, tmp_path):
        filepath = tmp_path / "notes.md"
        filepath.touch()

        with patch("subprocess.run") as mock_run:
            mock_add = MagicMock()
            mock_add.returncode = 0
            mock_commit = MagicMock()
            mock_commit.returncode = 1
            mock_commit.stderr = "Author identity unknown"
            mock_commit.stdout = ""
            mock_run.side_effect = [mock_add, mock_commit]

            # Should not raise — write failure is recoverable
            _git_commit(tmp_path, filepath, "test: update notes")

        assert mock_run.call_count == 2

    def test_git_not_installed_is_silent(self, tmp_path):
        filepath = tmp_path / "notes.md"
        filepath.touch()

        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            # Should not raise — stdio dev mode without git binary
            _git_commit(tmp_path, filepath, "test: update notes")


# ---------------------------------------------------------------------------
# Integration tests for register_knowledge_tools tool functions

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
def knowledge_tools(tmp_path):
    """Register knowledge tools and expose them via a dict keyed by function name."""
    from mcp_brain.tools.knowledge import register_knowledge_tools

    mcp = CapturingFastMCP()
    register_knowledge_tools(mcp, tmp_path)
    return mcp._tools, tmp_path


class TestKnowledgeRead:
    def test_file_not_found(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_read"]("school", "missing-project")
        assert "No knowledge file" in result

    def test_read_full_file(self, knowledge_tools):
        tools, base = knowledge_tools
        f = base / "school" / "notes.md"
        f.parent.mkdir(parents=True)
        f.write_text("## Overview\n\nSome content.\n", encoding="utf-8")
        result = tools["knowledge_read"]("school", "notes")
        assert "Some content." in result

    def test_read_specific_section(self, knowledge_tools):
        tools, base = knowledge_tools
        f = base / "work" / "project.md"
        f.parent.mkdir(parents=True)
        f.write_text("## Status\n\nIn progress.\n\n## Notes\n\nSome notes.\n", encoding="utf-8")
        result = tools["knowledge_read"]("work", "project", section="Status")
        assert "In progress." in result
        assert "Notes" not in result

    def test_section_not_found(self, knowledge_tools):
        tools, base = knowledge_tools
        f = base / "work" / "project.md"
        f.parent.mkdir(parents=True)
        f.write_text("## Status\n\nIn progress.\n", encoding="utf-8")
        result = tools["knowledge_read"]("work", "project", section="NonExistent")
        assert "not found" in result.lower() or "NonExistent" in result

    def test_invalid_scope_returns_error(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_read"]("...", "project")
        assert "Invalid" in result or "invalid" in result.lower()


class TestKnowledgeUpdate:
    def _mock_git(self):
        """Returns side_effect list for a successful git add + commit."""
        add = MagicMock()
        add.returncode = 0
        commit = MagicMock()
        commit.returncode = 0
        return [add, commit]

    def test_creates_new_file(self, knowledge_tools):
        tools, base = knowledge_tools
        with patch("subprocess.run", side_effect=self._mock_git()):
            result = tools["knowledge_update"]("school", "newproject", "Hardware", "Gate driver: TC4427\n")
        assert "Updated" in result
        filepath = base / "school" / "newproject.md"
        assert filepath.exists()
        assert "Gate driver" in filepath.read_text(encoding="utf-8")

    def test_updates_existing_section(self, knowledge_tools):
        tools, base = knowledge_tools
        f = base / "school" / "proj.md"
        f.parent.mkdir(parents=True)
        f.write_text("## Hardware\n\nOld content.\n\n## Notes\n\nKept.\n", encoding="utf-8")
        with patch("subprocess.run", side_effect=self._mock_git()):
            result = tools["knowledge_update"]("school", "proj", "Hardware", "New content.\n")
        assert "Updated" in result
        updated = f.read_text(encoding="utf-8")
        assert "New content." in updated
        assert "Kept." in updated  # other section preserved

    def test_invalid_scope_blocked(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_update"]("_meta", "file", "Section", "Bad write")
        assert "reserved" in result.lower() or "Refused" in result

    def test_content_gets_trailing_newline(self, knowledge_tools):
        tools, base = knowledge_tools
        with patch("subprocess.run", side_effect=self._mock_git()):
            tools["knowledge_update"]("work", "proj", "Section", "No newline")
        filepath = base / "work" / "proj.md"
        content = filepath.read_text(encoding="utf-8")
        assert "No newline\n" in content


class TestKnowledgeList:
    def test_empty_directory(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_list"]()
        assert "No knowledge files" in result

    def test_lists_files(self, knowledge_tools):
        tools, base = knowledge_tools
        (base / "school").mkdir()
        (base / "school" / "notes.md").write_text("# notes\n", encoding="utf-8")
        (base / "work").mkdir()
        (base / "work" / "project.md").write_text("# project\n", encoding="utf-8")
        result = tools["knowledge_list"]()
        assert "school/notes" in result
        assert "work/project" in result

    def test_scope_filter(self, knowledge_tools):
        tools, base = knowledge_tools
        (base / "school").mkdir()
        (base / "school" / "notes.md").write_text("# notes\n", encoding="utf-8")
        (base / "work").mkdir()
        (base / "work" / "project.md").write_text("# project\n", encoding="utf-8")
        result = tools["knowledge_list"](scope="school")
        assert "school/notes" in result
        assert "work/project" not in result

    def test_inbox_dir_excluded(self, knowledge_tools):
        tools, base = knowledge_tools
        (base / "inbox").mkdir()
        (base / "inbox" / "item.md").write_text("# item\n", encoding="utf-8")
        result = tools["knowledge_list"]()
        assert "inbox" not in result


class TestKnowledgeUndo:
    def test_steps_less_than_1_rejected(self, knowledge_tools):
        tools, _ = knowledge_tools
        result = tools["knowledge_undo"](steps=0)
        assert "Error" in result
        assert "1" in result

    def test_steps_more_than_10_rejected(self, knowledge_tools):
        tools, _ = knowledge_tools
        result = tools["knowledge_undo"](steps=11)
        assert "Error" in result
        assert "10" in result

    def test_git_not_installed(self, knowledge_tools):
        tools, _ = knowledge_tools
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_undo"](steps=1)
        assert "git not installed" in result.lower() or "Error" in result

    def test_no_commits_to_revert(self, knowledge_tools):
        tools, _ = knowledge_tools
        rev_list = MagicMock()
        rev_list.returncode = 0
        rev_list.stdout = "1\n"  # only 1 commit total
        with patch("subprocess.run", return_value=rev_list):
            result = tools["knowledge_undo"](steps=1)
        assert "Refused" in result or "empty" in result.lower()


class TestKnowledgeDelete:
    def test_file_not_found(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_delete"]("school", "nonexistent")
        assert "No knowledge file" in result

    def test_delete_existing_file(self, knowledge_tools):
        tools, base = knowledge_tools
        f = base / "school" / "proj.md"
        f.parent.mkdir(parents=True)
        f.write_text("# proj\n", encoding="utf-8")
        with patch("subprocess.run") as mock_run:
            rm = MagicMock()
            rm.returncode = 0
            commit = MagicMock()
            commit.returncode = 0
            mock_run.side_effect = [rm, commit]
            result = tools["knowledge_delete"]("school", "proj")
        assert "Deleted" in result
        assert not f.exists()

    def test_invalid_scope_blocked(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_delete"]("_meta", "file")
        assert "reserved" in result.lower() or "Refused" in result


class TestKnowledgeFreshness:
    def _make_file(self, base, scope, project, content="## Notes\n\nSome notes.\n"):
        d = base / scope
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{project}.md").write_text(content, encoding="utf-8")

    def test_empty_directory(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_freshness"]()
        assert "No knowledge files" in result

    def test_files_with_no_git(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_freshness"]()
        assert "notes" in result
        assert "no git" in result.lower() or "❓" in result

    def test_files_with_git_date(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes")
        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = "2026-04-01T12:00:00+00:00\n"
        with patch("subprocess.run", return_value=git_result):
            result = tools["knowledge_freshness"](scope="school")
        assert "notes" in result
        assert "Legend" in result

    def test_untracked_file(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes")
        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = ""  # empty means untracked
        with patch("subprocess.run", return_value=git_result):
            result = tools["knowledge_freshness"]()
        assert "notes" in result
        assert "❓" in result or "untracked" in result


class TestKnowledgeMap:
    def _make_file(self, base, scope, project, content="## Notes\n\nSome notes.\n"):
        d = base / scope
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{project}.md").write_text(content, encoding="utf-8")

    def test_empty_directory(self, knowledge_tools):
        tools, base = knowledge_tools
        result = tools["knowledge_map"]()
        assert "No knowledge files" in result

    def test_basic_map(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "power-electronics", "## Hardware\n\nGate driver.\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_map"]()
        assert "school" in result
        assert "power-electronics" in result
        assert "Hardware" in result  # section shown

    def test_scope_filter(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes")
        self._make_file(base, "work", "project")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_map"](scope="school")
        assert "school" in result
        assert "work" not in result

    def test_cross_references_shown(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes", "## Notes\n\nSee `work/project`.\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_map"]()
        assert "work/project" in result

    def test_sections_excluded_when_disabled(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "notes", "## Hardware\n\nContent.\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_map"](include_sections=False)
        assert "Hardware" not in result
        assert "notes" in result

    def test_summary_header_counts(self, knowledge_tools):
        tools, base = knowledge_tools
        self._make_file(base, "school", "a", "## Sec1\n\nBody.\n")
        self._make_file(base, "school", "b", "## Sec1\n\n## Sec2\n\nBody.\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = tools["knowledge_map"]()
        assert "2 files" in result
        assert "3 sections" in result


class TestKnowledgeUndoGitErrors:
    def test_rev_list_error(self, knowledge_tools):
        tools, _ = knowledge_tools
        from subprocess import CalledProcessError
        err = CalledProcessError(128, "git", stderr="not a git repo")
        with patch("subprocess.run", side_effect=err):
            result = tools["knowledge_undo"](steps=1)
        assert "Git error" in result or "Error" in result

    def test_successful_revert(self, knowledge_tools):
        tools, base = knowledge_tools
        count_result = MagicMock()
        count_result.returncode = 0
        count_result.stdout = "5\n"
        log_result = MagicMock()
        log_result.returncode = 0
        log_result.stdout = "abc1234 update school/notes § Hardware\n"
        revert_result = MagicMock()
        revert_result.returncode = 0
        with patch("subprocess.run", side_effect=[count_result, log_result, revert_result]):
            result = tools["knowledge_undo"](steps=1)
        assert "Reverted 1 commit" in result
