"""
Briefing tool — returns contextual summary from meta.yaml and relevant knowledge files.
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP


def register_briefing_tools(mcp: FastMCP, knowledge_dir: Path):

    @mcp.tool()
    def get_briefing(scope: str | None = None) -> str:
        """Get a contextual briefing based on meta.yaml and knowledge files.

        Use this at the start of a session to load relevant context.

        Args:
            scope: Optional — 'work', 'school', 'homelab'. If omitted, returns meta + overview.
        """
        meta_path = knowledge_dir / "meta.yaml"
        if not meta_path.exists():
            return "No meta.yaml found. Create one in the knowledge directory."

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        parts: list[str] = []

        # Always include core identity
        user = meta.get("user", {})
        parts.append(f"# Briefing for {user.get('name', 'User')}")
        parts.append(f"Timezone: {user.get('timezone', 'unknown')}")
        parts.append("")

        # Preferences
        prefs = meta.get("preferences", {})
        if prefs:
            pref_lines = [f"- {k}: {v}" for k, v in prefs.items()]
            parts.append("## Preferences")
            parts.extend(pref_lines)
            parts.append("")

        # Scope-specific or all
        scopes = [scope] if scope else list(meta.get("projects", {}).keys())

        for s in scopes:
            proj_meta = meta.get("projects", {}).get(s, {})
            if proj_meta:
                parts.append(f"## {s}")
                for k, v in proj_meta.items():
                    parts.append(f"- {k}: {v}")
                parts.append("")

            # List available knowledge files for this scope
            scope_dir = knowledge_dir / s
            if scope_dir.exists():
                files = sorted(scope_dir.glob("*.md"))
                if files:
                    parts.append(f"### Available knowledge files ({s}/)")
                    for f in files:
                        parts.append(f"- {f.stem}")
                    parts.append("")

        return "\n".join(parts)
