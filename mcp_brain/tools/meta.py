"""
Meta.yaml tools — read and update the main config file.

meta.yaml drives the briefing tool: user identity, preferences, and
project scope index. These tools expose it as raw YAML so the panel
can provide an editor UI without needing filesystem access.

Permissions:
  meta:read  — read current meta.yaml content
  meta:write — overwrite meta.yaml (validates YAML before writing)
"""

import subprocess
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import require


def _git_commit_meta(knowledge_dir: Path, meta_path: Path) -> None:
    """Auto-commit meta.yaml change (best-effort, same pattern as knowledge.py)."""
    try:
        add_result = subprocess.run(
            ["git", "add", str(meta_path)],
            cwd=knowledge_dir,
            capture_output=True,
            text=True,
        )
        if add_result.returncode != 0:
            return
        subprocess.run(
            ["git", "commit", "-m", "meta: update meta.yaml via panel", "--", str(meta_path)],
            cwd=knowledge_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pass  # git not installed — stdio dev mode


def register_meta_tools(mcp: FastMCP, knowledge_dir: Path) -> None:
    """Register meta_read and meta_update tools on *mcp*."""

    @mcp.tool(description="Read the raw content of meta.yaml (the briefing config).")
    def meta_read() -> str:
        """Return the raw YAML text of knowledge/meta.yaml."""
        try:
            require("meta:read")
        except PermissionDenied as e:
            return str(e)

        meta_path = knowledge_dir / "meta.yaml"
        if not meta_path.exists():
            return ""
        return meta_path.read_text(encoding="utf-8")

    @mcp.tool(
        description=(
            "Overwrite meta.yaml with new YAML content. "
            "Validates the YAML before writing. "
            "Args:\n"
            "  content: Full new YAML text for meta.yaml"
        )
    )
    def meta_update(content: str) -> str:
        """Validate and write new meta.yaml content."""
        try:
            require("meta:write")
        except PermissionDenied as e:
            return str(e)

        # Validate YAML before touching the file
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return f"Invalid YAML: {exc}"

        meta_path = knowledge_dir / "meta.yaml"
        meta_path.write_text(content, encoding="utf-8")
        _git_commit_meta(knowledge_dir, meta_path)
        return "meta.yaml updated."
