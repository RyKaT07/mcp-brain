"""Tests for mcp_brain.worker helpers.

Covers ``_load_integrations_env`` — the per-user credentials loader that reads
``STATE_DIR/integrations.env`` at worker startup.  The worker module itself
imports MCP/FastMCP heavily so we can only test the pure helper function
in isolation here; the conftest stubs make that safe.
"""

from __future__ import annotations

import os
from pathlib import Path


class TestLoadIntegrationsEnv:
    """Tests for mcp_brain.worker._load_integrations_env."""

    def _import(self):
        from mcp_brain.worker import _load_integrations_env
        return _load_integrations_env

    def test_missing_file_is_noop(self, tmp_path: Path, monkeypatch):
        """No file present → function returns silently without touching env."""
        load = self._import()
        before = dict(os.environ)
        load(tmp_path)
        assert dict(os.environ) == before

    def test_parses_key_value_lines(self, tmp_path: Path, monkeypatch):
        load = self._import()
        monkeypatch.delenv("TEST_FOO", raising=False)
        monkeypatch.delenv("TEST_BAR", raising=False)

        (tmp_path / "integrations.env").write_text(
            "TEST_FOO=hello\n"
            "TEST_BAR=world\n",
            encoding="utf-8",
        )

        load(tmp_path)
        assert os.environ["TEST_FOO"] == "hello"
        assert os.environ["TEST_BAR"] == "world"

        monkeypatch.delenv("TEST_FOO", raising=False)
        monkeypatch.delenv("TEST_BAR", raising=False)

    def test_overwrites_existing_env(self, tmp_path: Path, monkeypatch):
        """File values take priority over previously-set env vars.

        This is the whole point of the file-based approach — the Panel
        rewrites ``integrations.env`` when the user updates credentials, and
        the next worker spawn must see the new values even if the old ones
        were inherited from the container env.
        """
        load = self._import()
        monkeypatch.setenv("TEST_OVR", "old")
        (tmp_path / "integrations.env").write_text("TEST_OVR=new\n", encoding="utf-8")

        load(tmp_path)
        assert os.environ["TEST_OVR"] == "new"

        monkeypatch.delenv("TEST_OVR", raising=False)

    def test_blank_lines_and_comments_ignored(self, tmp_path: Path, monkeypatch):
        load = self._import()
        monkeypatch.delenv("TEST_KEY", raising=False)

        (tmp_path / "integrations.env").write_text(
            "\n"
            "# a comment line\n"
            "   \n"
            "TEST_KEY=present\n"
            "# trailing comment\n",
            encoding="utf-8",
        )

        load(tmp_path)
        assert os.environ["TEST_KEY"] == "present"
        # Comment keys must not leak as env vars.
        assert "# a comment line" not in os.environ
        monkeypatch.delenv("TEST_KEY", raising=False)

    def test_values_may_contain_equals(self, tmp_path: Path, monkeypatch):
        """Only the first ``=`` separates key from value — everything after is value."""
        load = self._import()
        monkeypatch.delenv("TEST_URL", raising=False)

        (tmp_path / "integrations.env").write_text(
            "TEST_URL=https://example.com/?a=1&b=2\n",
            encoding="utf-8",
        )

        load(tmp_path)
        assert os.environ["TEST_URL"] == "https://example.com/?a=1&b=2"
        monkeypatch.delenv("TEST_URL", raising=False)

    def test_malformed_lines_are_skipped(self, tmp_path: Path, monkeypatch, caplog):
        """Lines without ``=`` are logged and skipped, but don't abort the load."""
        load = self._import()
        monkeypatch.delenv("TEST_GOOD", raising=False)

        (tmp_path / "integrations.env").write_text(
            "not_a_kv_line\n"
            "TEST_GOOD=ok\n",
            encoding="utf-8",
        )

        load(tmp_path)
        assert os.environ["TEST_GOOD"] == "ok"
        monkeypatch.delenv("TEST_GOOD", raising=False)

    def test_empty_key_is_skipped(self, tmp_path: Path, monkeypatch):
        """Lines like ``=value`` have no useful key; do not pollute os.environ."""
        load = self._import()
        before = dict(os.environ)

        (tmp_path / "integrations.env").write_text("=orphan\n", encoding="utf-8")

        load(tmp_path)
        # Nothing should have changed.
        assert dict(os.environ) == before
