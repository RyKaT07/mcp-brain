"""Tests for mcp_brain.tools.inbox — helper functions and YAML staging logic."""

import uuid
from pathlib import Path

import pytest
import yaml

from mcp_brain.tools.inbox import _inbox_dir, _archive_dir, _find_item

INBOX_DIR_NAME = "inbox"
ARCHIVE_DIR_NAME = "inbox/_archive"


def _make_item(inbox: Path, *, status: str = "pending", source: str = "test") -> dict:
    """Write a test YAML item to the inbox and return the data dict."""
    item_id = f"test-{uuid.uuid4().hex[:8]}"
    data = {
        "id": item_id,
        "status": status,
        "source": source,
        "summary": f"Test item {item_id}",
        "content": "Some scraped content.",
        "scraped_at": "2026-01-01T00:00:00Z",
    }
    (inbox / f"{item_id}.yaml").write_text(yaml.dump(data), encoding="utf-8")
    return data


class TestInboxDir:
    def test_creates_inbox_dir(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        assert inbox.exists()
        assert inbox.is_dir()
        assert inbox == tmp_path / INBOX_DIR_NAME

    def test_idempotent(self, tmp_path):
        _inbox_dir(tmp_path)
        _inbox_dir(tmp_path)  # should not raise
        assert (tmp_path / INBOX_DIR_NAME).exists()


class TestArchiveDir:
    def test_creates_archive_dir(self, tmp_path):
        archive = _archive_dir(tmp_path)
        assert archive.exists()
        assert archive.is_dir()

    def test_archive_inside_inbox(self, tmp_path):
        archive = _archive_dir(tmp_path)
        # Archive must be inside the inbox dir
        assert str(tmp_path) in str(archive)


class TestFindItem:
    def test_find_existing_item(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        item = _make_item(inbox)
        item_id = item["id"]

        path, data = _find_item(inbox, item_id)
        assert path is not None
        assert data is not None
        assert data["id"] == item_id

    def test_find_nonexistent_item(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        path, data = _find_item(inbox, "nonexistent-item-id")
        assert path is None
        assert data is None

    def test_find_among_multiple(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        items = [_make_item(inbox) for _ in range(5)]
        target = items[2]

        path, data = _find_item(inbox, target["id"])
        assert data is not None
        assert data["id"] == target["id"]

    def test_find_item_with_different_status(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        item = _make_item(inbox, status="accepted")

        path, data = _find_item(inbox, item["id"])
        assert data is not None
        assert data["status"] == "accepted"

    def test_find_item_by_source(self, tmp_path):
        inbox = _inbox_dir(tmp_path)
        item_a = _make_item(inbox, source="university_portal")
        item_b = _make_item(inbox, source="discord")

        _, data_a = _find_item(inbox, item_a["id"])
        _, data_b = _find_item(inbox, item_b["id"])

        assert data_a["source"] == "university_portal"
        assert data_b["source"] == "discord"
