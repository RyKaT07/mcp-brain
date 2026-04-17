"""
Secrets schema tool — knows WHAT secrets exist and WHERE they're stored,
but NEVER the actual values.
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import ALL, allowed_subscopes, require


def register_secrets_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def secrets_schema(scope: str | None = None) -> str:
        """Look up what secrets/credentials exist and where they are stored.

        This tool returns the schema (key names + storage location) but NEVER actual values.

        Args:
            scope: Optional filter — e.g. 'ovh', 'homelab'. If omitted, lists all.
        """
        if scope is not None:
            try:
                require(f"secrets_schema:{scope}")
            except PermissionDenied as e:
                return str(e)

        meta_path = knowledge_dir / "meta.yaml"
        if not meta_path.exists():
            return "No meta.yaml found."

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        schema = meta.get("secrets_schema", {})

        if not schema:
            return "No secrets_schema defined in meta.yaml."

        if scope:
            entry = schema.get(scope)
            if not entry:
                return f"No secrets schema for scope '{scope}'. Available: {', '.join(schema.keys())}"
            lines = [f"## {scope}"]
            lines.append(f"Location: {entry.get('location', 'unknown')}")
            lines.append(f"Keys: {', '.join(entry.get('keys', []))}")
            return "\n".join(lines)

        # List all (filtered by token's secrets_schema:* permissions)
        allowed = allowed_subscopes("secrets_schema")
        parts: list[str] = []
        for name, entry in schema.items():
            if allowed is not ALL and name not in allowed:
                continue
            parts.append(f"## {name}")
            parts.append(f"Location: {entry.get('location', 'unknown')}")
            parts.append(f"Keys: {', '.join(entry.get('keys', []))}")
            parts.append("")

        if not parts:
            return "Permission denied: no secrets_schema scopes available to this token."

        return "\n".join(parts)
