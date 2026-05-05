"""Tests for mcp_brain.tools.maintain — session lifecycle and hint extraction."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_brain.tools.maintain import (
    MAX_QUESTIONS,
    SESSION_FILENAME,
    _delete_session,
    _format_done_summary,
    _format_next_question,
    _generate_questions_cheap,
    _get_session_path,
    _load_session,
    _parse_sections,
    _rebuild_markdown,
    _save_session,
    extract_content_hints,
)


# ---------------------------------------------------------------------------
# extract_content_hints

class TestExtractContentHints:
    def test_iso_date_older_than_30_days_flagged(self):
        content = "Last updated: 2020-01-15\nSome content."
        hints = extract_content_hints(content)
        date_hints = [h for h in hints if h["type"] == "date"]
        assert any(h["value"] == "2020-01-15" for h in date_hints)

    def test_recent_date_not_flagged(self):
        # Use a very recent date — this year
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        content = f"Updated: {recent}\nContent."
        hints = extract_content_hints(content)
        date_hints = [h for h in hints if h["type"] == "date"]
        assert not any(h["value"] == recent for h in date_hints)

    def test_version_strings_detected(self):
        content = "Using Python v3.11 and Node 18.0.1"
        hints = extract_content_hints(content)
        version_hints = [h for h in hints if h["type"] == "version"]
        values = [h["value"] for h in version_hints]
        assert any("3.11" in v for v in values)
        assert any("18.0.1" in v for v in values)

    def test_todo_marker_detected(self):
        content = "Normal line\nTODO: update this section\nAnother line"
        hints = extract_content_hints(content)
        todo_hints = [h for h in hints if h["type"] == "todo"]
        assert len(todo_hints) == 1
        assert "TODO" in todo_hints[0]["value"]

    def test_fixme_marker_detected(self):
        content = "FIXME: broken link here"
        hints = extract_content_hints(content)
        todo_hints = [h for h in hints if h["type"] == "todo"]
        assert any("FIXME" in h["value"] for h in todo_hints)

    def test_line_numbers_correct(self):
        content = "line one\nTODO: fix me\nline three"
        hints = extract_content_hints(content)
        todo_hints = [h for h in hints if h["type"] == "todo"]
        assert todo_hints[0]["line_number"] == 2

    def test_empty_content_returns_empty(self):
        assert extract_content_hints("") == []

    def test_no_hints_in_clean_content(self):
        content = "# Clean file\n\nThis file has no dates or TODOs.\n"
        hints = extract_content_hints(content)
        # May have version hints from things like "no" → no, but no date/todo
        todo_hints = [h for h in hints if h["type"] in ("date", "todo")]
        assert not todo_hints

    def test_days_ago_included_for_dates(self):
        content = "Updated 2020-06-01"
        hints = extract_content_hints(content)
        date_hints = [h for h in hints if h["type"] == "date"]
        assert date_hints
        assert "days_ago" in date_hints[0]
        assert date_hints[0]["days_ago"] > 30


# ---------------------------------------------------------------------------
# _generate_questions_cheap

class TestGenerateQuestionsCheap:
    def _make_stale_file(self, path="work/job", days=60, hints=None):
        return {"path": path, "days_stale": days, "hints": hints or []}

    def test_returns_one_question_per_file(self):
        files = [self._make_stale_file("work/a"), self._make_stale_file("work/b")]
        questions = _generate_questions_cheap(files)
        assert len(questions) == 2

    def test_question_includes_file_path(self):
        files = [self._make_stale_file("school/physics")]
        questions = _generate_questions_cheap(files)
        assert questions[0]["file"] == "school/physics"

    def test_question_includes_hint_context(self):
        hints = [{"type": "version", "value": "v3.11", "line_number": 5}]
        files = [self._make_stale_file("work/tools", hints=hints)]
        questions = _generate_questions_cheap(files)
        assert "v3.11" in questions[0]["question"]

    def test_no_hints_uses_stale_days(self):
        files = [self._make_stale_file("work/old", days=120)]
        questions = _generate_questions_cheap(files)
        assert "120" in questions[0]["question"]

    def test_empty_files_returns_empty(self):
        assert _generate_questions_cheap([]) == []


# ---------------------------------------------------------------------------
# Session helpers

class TestSessionHelpers:
    def test_save_and_load_roundtrip(self, tmp_path):
        session = {
            "session_id": "abc123",
            "scope": "work",
            "questions": [],
            "current_question_index": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        path = tmp_path / SESSION_FILENAME
        _save_session(path, session)
        loaded = _load_session(path)
        assert loaded == session

    def test_load_missing_returns_none(self, tmp_path):
        path = tmp_path / SESSION_FILENAME
        assert _load_session(path) is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / SESSION_FILENAME
        path.write_text("{not valid json", encoding="utf-8")
        assert _load_session(path) is None

    def test_delete_removes_file(self, tmp_path):
        path = tmp_path / SESSION_FILENAME
        path.write_text("{}", encoding="utf-8")
        _delete_session(path)
        assert not path.exists()

    def test_delete_missing_file_is_noop(self, tmp_path):
        path = tmp_path / SESSION_FILENAME
        _delete_session(path)  # should not raise


# ---------------------------------------------------------------------------
# Session display helpers

class TestFormatNextQuestion:
    def _make_session(self, idx=0, questions=None):
        if questions is None:
            questions = [
                {"id": 1, "file": "work/job", "question": "Is this current?", "status": "pending", "proposed_change": None},
                {"id": 2, "file": "school/math", "question": "Still valid?", "status": "pending", "proposed_change": None},
            ]
        return {
            "session_id": "sid-001",
            "questions": questions,
            "current_question_index": idx,
        }

    def test_shows_correct_question_number(self):
        session = self._make_session(idx=0)
        result = _format_next_question(session)
        assert "Question 1/2" in result

    def test_shows_file_path(self):
        session = self._make_session(idx=0)
        result = _format_next_question(session)
        assert "work/job" in result

    def test_shows_session_id(self):
        session = self._make_session(idx=0)
        result = _format_next_question(session)
        assert "sid-001" in result

    def test_at_end_returns_done_summary(self):
        session = self._make_session(idx=2)  # past last question
        result = _format_next_question(session)
        assert "complete" in result.lower()


class TestFormatDoneSummary:
    def test_counts_confirmed(self):
        questions = [
            {"status": "confirmed"},
            {"status": "confirmed"},
            {"status": "skipped"},
        ]
        session = {"questions": questions, "session_id": "x"}
        result = _format_done_summary(session)
        assert "2" in result  # 2 confirmed

    def test_counts_skipped(self):
        questions = [{"status": "skipped"}, {"status": "pending"}]
        session = {"questions": questions, "session_id": "x"}
        result = _format_done_summary(session)
        assert "1" in result  # 1 skipped

    def test_shows_session_complete(self):
        session = {"questions": [], "session_id": "x"}
        result = _format_done_summary(session)
        assert "complete" in result.lower()


# ---------------------------------------------------------------------------
# _parse_sections and _rebuild_markdown (internal helpers)

class TestParseSections:
    def test_parses_h2_sections(self):
        content = "# Title\n\n## Alpha\nalpha content\n## Beta\nbeta content\n"
        sections = _parse_sections(content)
        assert "Alpha" in sections
        assert "Beta" in sections
        assert "alpha content" in sections["Alpha"]

    def test_preamble_captured(self):
        content = "# Title\n\nsome preamble\n\n## Section\ncontent\n"
        sections = _parse_sections(content)
        assert "_preamble" in sections
        assert "Title" in sections["_preamble"]

    def test_rebuild_roundtrip(self):
        content = "# Proj\n\n## Alpha\nalpha body\n## Beta\nbeta body\n"
        sections = _parse_sections(content)
        rebuilt = _rebuild_markdown(sections)
        # Should contain the sections
        assert "Alpha" in rebuilt
        assert "alpha body" in rebuilt


# ---------------------------------------------------------------------------
# MAX_QUESTIONS cap

class TestMaxQuestionsCap:
    def test_cheap_mode_capped(self):
        files = [{"path": f"work/f{i}", "days_stale": 100, "hints": []} for i in range(20)]
        questions = _generate_questions_cheap(files)
        # _generate_questions_cheap itself doesn't cap — that's done in maintain_start
        # Just verify it generates one per file (cap applied elsewhere)
        assert len(questions) == 20

    def test_max_questions_constant(self):
        assert MAX_QUESTIONS == 10


# ---------------------------------------------------------------------------
# maintain_last_run + audit cache


class _CapturingFastMCP:
    """Minimal MCP stand-in that captures registered tool functions by name."""

    def __init__(self):
        self._tools: dict = {}

    def tool(self, description=None, **kwargs):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def maintain_tools(tmp_path):
    from mcp_brain.tools.maintain import register_maintain_tools

    mcp = _CapturingFastMCP()
    register_maintain_tools(mcp, tmp_path)
    return mcp._tools, tmp_path


class TestAuditCachePath:
    def test_root_cache_path(self, tmp_path: Path):
        from mcp_brain.tools.maintain import _audit_cache_path

        assert _audit_cache_path(tmp_path, None) == (
            tmp_path / "_index" / "last_audit.json"
        )

    def test_per_user_cache_path(self, tmp_path: Path):
        from mcp_brain.tools.maintain import _audit_cache_path

        assert _audit_cache_path(tmp_path, "alice") == (
            tmp_path / "users" / "alice" / "_index" / "last_audit.json"
        )


class TestMaintainLastRun:
    def test_first_call_runs_audit_and_writes_cache(
        self, maintain_tools, tmp_path: Path
    ):
        tools, base = maintain_tools
        (base / "homelab").mkdir()
        (base / "homelab" / "alpha.md").write_text(
            "## Overview\nfresh", encoding="utf-8"
        )
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            raw = tools["maintain_last_run"]()
        out = json.loads(raw)
        assert "report" in out
        assert "Knowledge Vault Maintenance Report" in out["report"]
        assert out["refreshed"] is True
        cache = base / "_index" / "last_audit.json"
        assert cache.exists()
        cached = json.loads(cache.read_text(encoding="utf-8"))
        assert cached["report"] == out["report"]

    def test_second_call_returns_cached(
        self, maintain_tools, tmp_path: Path
    ):
        tools, base = maintain_tools
        (base / "homelab").mkdir()
        (base / "homelab" / "alpha.md").write_text(
            "## A\n", encoding="utf-8"
        )
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            first = json.loads(tools["maintain_last_run"]())
            (base / "homelab" / "beta.md").write_text(
                "## B\n", encoding="utf-8"
            )
            second = json.loads(tools["maintain_last_run"]())
        assert second["refreshed"] is False
        assert second["report"] == first["report"]
        assert second["timestamp"] == first["timestamp"]

    def test_force_bypasses_cache(self, maintain_tools, tmp_path: Path):
        tools, base = maintain_tools
        (base / "homelab").mkdir()
        (base / "homelab" / "a.md").write_text("## A\n", encoding="utf-8")
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            first = json.loads(tools["maintain_last_run"]())
            second = json.loads(tools["maintain_last_run"](force=True))
        assert first["refreshed"] is True
        assert second["refreshed"] is True

    def test_envelope_shape(self, maintain_tools, tmp_path: Path):
        tools, base = maintain_tools
        (base / "homelab").mkdir()
        (base / "homelab" / "a.md").write_text("## A\n", encoding="utf-8")
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            raw = tools["maintain_last_run"]()
        out = json.loads(raw)
        assert set(out.keys()) == {
            "timestamp",
            "report",
            "refreshed",
            "ttl_seconds",
        }
        assert out["ttl_seconds"] == 24 * 3600
