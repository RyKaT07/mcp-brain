"""
Secrets schema tool — knows WHAT secrets exist and WHERE they're stored,
but NEVER the actual values.
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP


def register_secrets_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool()
    def secrets_schema(scope: str | None = None) -> str:
        """Look up what secrets/credentials exist and where they are stored.

        This tool returns the schema (key names + storage location) but NEVER actual values.

        Args:
            scope: Optional filter — e.g. 'ovh', 'homelab'. If omitted, lists all.
        """
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

        # List all
        parts: list[str] = []
        for name, entry in schema.items():
            parts.append(f"## {name}")
            parts.append(f"Location: {entry.get('location', 'unknown')}")
            parts.append(f"Keys: {', '.join(entry.get('keys', []))}")
            parts.append("")

        return "\n".join(parts)
