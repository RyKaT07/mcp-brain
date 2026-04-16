"""Tests for briefing config parsing and generation."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out yaml (may not be installed in CI) and mcp stubs already in conftest

try:
    import yaml  # noqa: F401
except ImportError:
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda s: {}
    sys.modules.setdefault("yaml", yaml_stub)

# ---------------------------------------------------------------------------
# Import briefing module (conftest has already stubbed mcp.*)

from mcp_brain.tools.briefing import (
    BriefingConfig,
    _format_calendar_section,
    _format_knowledge_section,
    _format_tasks_section,
    _format_trello_section,
    _get_recent_knowledge_changes,
    _parse_briefing_config,
    generate_briefing,
)


# ---------------------------------------------------------------------------
# BriefingConfig defaults

class TestBriefingConfig:
    def test_defaults(self):
        cfg = BriefingConfig()
        assert cfg.enabled is True
        assert cfg.lookahead_days == 1
        assert cfg.format_length == "standard"
        assert cfg.sources["calendar"] is True
        assert cfg.sources["trello"] is False
        assert "calendar" in cfg.sections

    def test_custom_values(self):
        cfg = BriefingConfig(lookahead_days=3, format_length="concise")
        assert cfg.lookahead_days == 3
        assert cfg.format_length == "concise"


# ---------------------------------------------------------------------------
# _parse_briefing_config

class TestParseBriefingConfig:
    def test_returns_none_when_no_key(self):
        meta = {"user": {"name": "Pat"}}
        assert _parse_briefing_config(meta) is None

    def test_returns_none_when_briefing_not_dict(self):
        meta = {"briefing": True}
        assert _parse_briefing_config(meta) is None

    def test_parses_top_level_config(self):
        meta = {"briefing": {"lookahead_days": 3, "format_length": "concise"}}
        cfg = _parse_briefing_config(meta)
        assert cfg is not None
        assert cfg.lookahead_days == 3
        assert cfg.format_length == "concise"

    def test_scope_level_config_fallback(self):
        """Config under projects.<scope>.briefing works when no global config exists."""
        meta = {
            "projects": {
                "work": {"briefing": {"lookahead_days": 2}}
            }
        }
        cfg = _parse_briefing_config(meta, scope="work")
        assert cfg is not None
        assert cfg.lookahead_days == 2

    def test_scope_overrides_global(self):
        """Scope-level briefing keys override the top-level briefing config."""
        meta = {
            "briefing": {"lookahead_days": 1, "format_length": "standard"},
            "projects": {
                "work": {"briefing": {"lookahead_days": 7}}
            }
        }
        cfg = _parse_briefing_config(meta, scope="work")
        assert cfg is not None
        assert cfg.lookahead_days == 7          # overridden
        assert cfg.format_length == "standard"  # inherited

    def test_unknown_keys_ignored(self):
        meta = {"briefing": {"lookahead_days": 2, "nonexistent_key": "foo"}}
        cfg = _parse_briefing_config(meta)
        assert cfg is not None
        assert not hasattr(cfg, "nonexistent_key")

    def test_enabled_false(self):
        meta = {"briefing": {"enabled": False}}
        cfg = _parse_briefing_config(meta)
        assert cfg is not None
        assert cfg.enabled is False

    def test_sections_list(self):
        meta = {"briefing": {"sections": ["tasks", "trello"]}}
        cfg = _parse_briefing_config(meta)
        assert cfg is not None
        assert cfg.sections == ["tasks", "trello"]


# ---------------------------------------------------------------------------
# Section formatters

class TestFormatCalendarSection:
    def _event(self, title, start, end=None):
        e = {"summary": title, "start": {"dateTime": start}}
        if end:
            e["end"] = {"dateTime": end}
        return e

    def test_empty_returns_none(self):
        cfg = BriefingConfig()
        assert _format_calendar_section([], cfg) is None

    def test_formats_events(self):
        cfg = BriefingConfig()
        events = [self._event("Standup", "2026-04-17T09:00:00+02:00", "2026-04-17T09:30:00+02:00")]
        result = _format_calendar_section(events, cfg)
        assert result is not None
        assert "Standup" in result
        assert "09:00" in result
        assert "## Calendar" in result

    def test_concise_limits_to_5(self):
        cfg = BriefingConfig(format_length="concise")
        events = [self._event(f"Event {i}", "2026-04-17T09:00:00+02:00") for i in range(10)]
        result = _format_calendar_section(events, cfg)
        assert result is not None
        # Should only include 5 events
        assert result.count("- ") == 5


class TestFormatTasksSection:
    def _task(self, content, due_date=None):
        t = {"content": content}
        if due_date:
            t["due"] = {"date": due_date}
        return t

    def test_empty_returns_none(self):
        cfg = BriefingConfig()
        assert _format_tasks_section([], cfg) is None

    def test_formats_tasks(self):
        cfg = BriefingConfig()
        tasks = [self._task("Write tests", "2026-04-17")]
        result = _format_tasks_section(tasks, cfg)
        assert result is not None
        assert "Write tests" in result
        assert "## Tasks" in result
        assert "2026-04-17" in result

    def test_concise_limits_to_5(self):
        cfg = BriefingConfig(format_length="concise")
        tasks = [self._task(f"Task {i}") for i in range(10)]
        result = _format_tasks_section(tasks, cfg)
        assert result is not None
        assert result.count("- ") == 5


class TestFormatKnowledgeSection:
    def test_empty_returns_none(self):
        cfg = BriefingConfig()
        assert _format_knowledge_section([], cfg) is None

    def test_formats_changes(self):
        cfg = BriefingConfig()
        changes = ["work/projects.md", "school/notes.md"]
        result = _format_knowledge_section(changes, cfg)
        assert result is not None
        assert "work/projects.md" in result
        assert "## Recent Knowledge Updates" in result


class TestFormatTrelloSection:
    def _card(self, name, board="Board"):
        return {"name": name, "_board": board}

    def test_empty_returns_none(self):
        cfg = BriefingConfig()
        assert _format_trello_section([], cfg) is None

    def test_formats_cards(self):
        cfg = BriefingConfig()
        cards = [self._card("Deploy infra", "DevOps")]
        result = _format_trello_section(cards, cfg)
        assert result is not None
        assert "Deploy infra" in result
        assert "## Trello" in result


# ---------------------------------------------------------------------------
# _get_recent_knowledge_changes

class TestGetRecentKnowledgeChanges:
    def test_returns_empty_on_git_not_found(self, tmp_path):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _get_recent_knowledge_changes(tmp_path, None, 1)
        assert result == []

    def test_returns_empty_on_nonzero_returncode(self, tmp_path):
        mock = MagicMock()
        mock.returncode = 1
        mock.stdout = ""
        with patch("subprocess.run", return_value=mock):
            result = _get_recent_knowledge_changes(tmp_path, None, 1)
        assert result == []

    def test_parses_filenames(self, tmp_path):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "work/projects.md\n\nwork/projects.md\nschool/notes.md\n"
        with patch("subprocess.run", return_value=mock):
            result = _get_recent_knowledge_changes(tmp_path, "work", 7)
        # Deduplicates and preserves order
        assert result == ["work/projects.md", "school/notes.md"]


# ---------------------------------------------------------------------------
# generate_briefing

class TestGenerateBriefing:
    def test_skips_calendar_without_credentials(self, tmp_path):
        cfg = BriefingConfig(sections=["calendar"])
        result = generate_briefing(None, cfg, tmp_path)
        # No credentials → no calendar data → no data available message
        assert "No briefing data available" in result

    def test_knowledge_updates_uses_git(self, tmp_path):
        cfg = BriefingConfig(sections=["knowledge_updates"], sources={"knowledge_updates": True})
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "work/projects.md\n"
        with patch("subprocess.run", return_value=mock):
            result = generate_briefing(None, cfg, tmp_path)
        assert "Recent Knowledge Updates" in result
        assert "work/projects.md" in result

    def test_show_empty_sections(self, tmp_path):
        cfg = BriefingConfig(
            sections=["calendar"],
            sources={"calendar": True},
            show_empty_sections=True,
        )
        # No credentials → empty calendar, but show_empty_sections=True
        result = generate_briefing(None, cfg, tmp_path)
        assert "no data" in result.lower()

    def test_tasks_with_mocked_api(self, tmp_path):
        cfg = BriefingConfig(sections=["tasks"], sources={"tasks": True})
        fake_tasks = [{"content": "Buy milk", "due": None}]
        with patch("mcp_brain.tools.briefing._fetch_todoist_tasks", return_value=fake_tasks):
            result = generate_briefing(None, cfg, tmp_path, todoist_api_key="fake")
        assert "Buy milk" in result
        assert "## Tasks" in result

    def test_trello_with_mocked_api(self, tmp_path):
        cfg = BriefingConfig(sections=["trello"], sources={"trello": True})
        fake_cards = [{"name": "Fix login bug", "_board": "Dev", "due": None}]
        with patch("mcp_brain.tools.briefing._fetch_trello_cards", return_value=fake_cards):
            result = generate_briefing(
                None, cfg, tmp_path,
                trello_api_key="fake", trello_api_token="fake",
            )
        assert "Fix login bug" in result
        assert "## Trello" in result

    def test_multiple_sections_combined(self, tmp_path):
        cfg = BriefingConfig(
            sections=["tasks", "knowledge_updates"],
            sources={"tasks": True, "knowledge_updates": True},
        )
        fake_tasks = [{"content": "Review PR", "due": None}]
        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = "work/notes.md\n"
        with patch("mcp_brain.tools.briefing._fetch_todoist_tasks", return_value=fake_tasks), \
             patch("subprocess.run", return_value=mock_git):
            result = generate_briefing(None, cfg, tmp_path, todoist_api_key="fake")
        assert "## Tasks" in result
        assert "## Recent Knowledge Updates" in result
