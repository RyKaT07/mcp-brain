"""Tests for mcp_brain.auth — token verification, scope matching, hot-reload."""

import textwrap
from pathlib import Path

import pytest
import yaml

from mcp_brain.auth import AuthConfig, TokenEntry, YamlTokenVerifier, match_scope


# ---------------------------------------------------------------------------
# match_scope

class TestMatchScope:
    def test_god_mode_matches_anything(self):
        assert match_scope("*", "knowledge:read:school")
        assert match_scope("*", "inbox:write")
        assert match_scope("*", "totally:made:up:scope")

    def test_exact_match(self):
        assert match_scope("inbox:read", "inbox:read")
        assert match_scope("knowledge:read:school", "knowledge:read:school")

    def test_wildcard_middle_segment(self):
        assert match_scope("knowledge:*:school", "knowledge:read:school")
        assert match_scope("knowledge:*:school", "knowledge:write:school")
        assert not match_scope("knowledge:*:school", "knowledge:read:work")

    def test_wildcard_last_segment(self):
        assert match_scope("knowledge:read:*", "knowledge:read:school")
        assert match_scope("knowledge:read:*", "knowledge:read:homelab")
        assert not match_scope("knowledge:read:*", "knowledge:write:school")

    def test_wildcard_all_except_first(self):
        assert match_scope("knowledge:*:*", "knowledge:read:school")
        assert match_scope("knowledge:*:*", "knowledge:write:work")
        assert not match_scope("knowledge:*:*", "inbox:read")

    def test_length_mismatch_no_match(self):
        # Two-segment granted vs three-segment required
        assert not match_scope("knowledge:read", "knowledge:read:school")
        # Three-segment granted vs two-segment required
        assert not match_scope("knowledge:read:school", "inbox:read")

    def test_no_match_different_resource(self):
        assert not match_scope("inbox:read", "knowledge:read:school")
        assert not match_scope("briefing:work", "inbox:read")

    def test_exact_two_segment(self):
        assert match_scope("inbox:read", "inbox:read")
        assert match_scope("inbox:write", "inbox:write")
        assert not match_scope("inbox:read", "inbox:write")

    def test_wildcard_two_segment(self):
        assert match_scope("inbox:*", "inbox:read")
        assert match_scope("inbox:*", "inbox:write")
        assert not match_scope("inbox:*", "briefing:work")


# ---------------------------------------------------------------------------
# AuthConfig / TokenEntry

class TestAuthConfig:
    def _write_config(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "auth.yaml"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def test_load_valid_config(self, tmp_path):
        p = self._write_config(tmp_path, """
            tokens:
              - id: admin
                token: "tok_admin_secret_long"
                scopes: ["*"]
              - id: reader
                token: "tok_reader_secret_long"
                scopes:
                  - knowledge:read:work
                  - inbox:read
        """)
        cfg = AuthConfig.load(p)
        assert len(cfg.tokens) == 2
        assert cfg.tokens[0].id == "admin"
        assert cfg.tokens[1].scopes == ["knowledge:read:work", "inbox:read"]

    def test_load_empty_config(self, tmp_path):
        p = tmp_path / "auth.yaml"
        p.write_text("", encoding="utf-8")
        cfg = AuthConfig.load(p)
        assert cfg.tokens == []

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            AuthConfig.load(tmp_path / "nonexistent.yaml")

    def test_by_token_index(self, tmp_path):
        p = self._write_config(tmp_path, """
            tokens:
              - id: a
                token: "tok_aaa_secret_token"
                scopes: ["*"]
              - id: b
                token: "tok_bbb_secret_token"
                scopes: ["inbox:read"]
        """)
        cfg = AuthConfig.load(p)
        index = cfg.by_token()
        assert "tok_aaa_secret_token" in index
        assert index["tok_aaa_secret_token"].id == "a"
        assert "tok_bbb_secret_token" in index

    def test_token_too_short_rejected(self, tmp_path):
        p = self._write_config(tmp_path, """
            tokens:
              - id: short
                token: "abc"
                scopes: ["*"]
        """)
        with pytest.raises(Exception):  # pydantic ValidationError
            AuthConfig.load(p)

    def test_user_id_optional(self, tmp_path):
        p = self._write_config(tmp_path, """
            tokens:
              - id: user-scoped
                token: "tok_user_scoped_12345"
                user_id: "alice"
                scopes: ["*"]
              - id: global
                token: "tok_global_12345678"
                scopes: ["*"]
        """)
        cfg = AuthConfig.load(p)
        assert cfg.tokens[0].user_id == "alice"
        assert cfg.tokens[1].user_id is None


# ---------------------------------------------------------------------------
# YamlTokenVerifier

class TestYamlTokenVerifier:
    def _make_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "auth.yaml"
        p.write_text(textwrap.dedent("""
            tokens:
              - id: full-access
                token: "tok_full_access_secret"
                scopes: ["*"]
              - id: read-only
                token: "tok_read_only_secret"
                scopes:
                  - knowledge:read:work
                  - inbox:read
        """), encoding="utf-8")
        return p

    @pytest.mark.asyncio
    async def test_verify_known_token(self, tmp_path):
        p = self._make_config(tmp_path)
        verifier = YamlTokenVerifier(p, reload_interval=60)
        result = await verifier.verify_token("tok_full_access_secret")
        assert result is not None
        assert result.client_id == "full-access"
        assert "*" in result.scopes

    @pytest.mark.asyncio
    async def test_verify_unknown_token_returns_none(self, tmp_path):
        p = self._make_config(tmp_path)
        verifier = YamlTokenVerifier(p, reload_interval=60)
        result = await verifier.verify_token("tok_nonexistent_token")
        assert result is None

    @pytest.mark.asyncio
    async def test_verify_read_only_token_scopes(self, tmp_path):
        p = self._make_config(tmp_path)
        verifier = YamlTokenVerifier(p, reload_interval=60)
        result = await verifier.verify_token("tok_read_only_secret")
        assert result is not None
        assert result.client_id == "read-only"
        assert "knowledge:read:work" in result.scopes
        assert "inbox:read" in result.scopes
        assert "*" not in result.scopes

    @pytest.mark.asyncio
    async def test_hot_reload_adds_token(self, tmp_path):
        p = self._make_config(tmp_path)
        verifier = YamlTokenVerifier(p, reload_interval=60)

        # No new token yet
        assert await verifier.verify_token("tok_new_token_added") is None

        # Add a new token to the config
        p.write_text(textwrap.dedent("""
            tokens:
              - id: full-access
                token: "tok_full_access_secret"
                scopes: ["*"]
              - id: new-token
                token: "tok_new_token_added"
                scopes: ["inbox:read"]
        """), encoding="utf-8")
        verifier._load()  # force reload

        result = await verifier.verify_token("tok_new_token_added")
        assert result is not None
        assert result.client_id == "new-token"

    @pytest.mark.asyncio
    async def test_hot_reload_removes_token(self, tmp_path):
        p = self._make_config(tmp_path)
        verifier = YamlTokenVerifier(p, reload_interval=60)

        assert await verifier.verify_token("tok_read_only_secret") is not None

        # Remove the read-only token
        p.write_text(textwrap.dedent("""
            tokens:
              - id: full-access
                token: "tok_full_access_secret"
                scopes: ["*"]
        """), encoding="utf-8")
        verifier._load()

        assert await verifier.verify_token("tok_read_only_secret") is None
