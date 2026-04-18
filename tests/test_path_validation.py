"""Tests for require_path_within path validation helper."""

import os
from pathlib import Path

import pytest


def test_normal_path_passes(tmp_path: Path):
    from mcp_brain.tools._perms import require_path_within

    base = tmp_path / "knowledge"
    base.mkdir()
    target = base / "work" / "project.md"
    target.parent.mkdir(parents=True)
    target.touch()

    # Should not raise
    require_path_within(target, base)


def test_traversal_path_raises(tmp_path: Path):
    from mcp_brain.auth import PermissionDenied
    from mcp_brain.tools._perms import require_path_within

    base = tmp_path / "knowledge"
    base.mkdir()
    # Path that escapes via ../
    escaped = base / ".." / "etc" / "passwd"

    with pytest.raises(PermissionDenied):
        require_path_within(escaped, base)


def test_symlink_escape_raises(tmp_path: Path):
    from mcp_brain.auth import PermissionDenied
    from mcp_brain.tools._perms import require_path_within

    base = tmp_path / "knowledge"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("sensitive data")

    # Create symlink inside knowledge tree pointing outside
    link = base / "evil.md"
    link.symlink_to(secret)

    with pytest.raises(PermissionDenied):
        require_path_within(link, base)


def test_nested_valid_path_passes(tmp_path: Path):
    from mcp_brain.tools._perms import require_path_within

    base = tmp_path / "knowledge"
    nested = base / "users" / "alice" / "work" / "project.md"
    nested.parent.mkdir(parents=True)
    nested.touch()

    # Should not raise
    require_path_within(nested, base)


def test_resolve_file_rejects_symlink_escape(tmp_path: Path):
    """Integration test: _resolve_file in knowledge.py catches symlink escapes."""
    from mcp_brain.tools.knowledge import _resolve_file

    base = tmp_path / "knowledge"
    scope_dir = base / "work"
    scope_dir.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "evil.md"
    secret.write_text("sensitive")

    # Plant a symlink: knowledge/work/evil.md -> ../outside/evil.md
    link = scope_dir / "evil.md"
    link.symlink_to(secret)

    with pytest.raises(ValueError, match="escapes knowledge directory"):
        _resolve_file(base, "work", "evil")
