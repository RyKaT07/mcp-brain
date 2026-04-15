"""
Briefing tool — returns contextual summary from meta.yaml and relevant knowledge files.
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import ALL, allowed_subscopes, require


def _sanitize_meta_value(v: object) -> str:
    """Sanitize a meta.yaml value before embedding in briefing output.

    Strips newlines and carriage returns to prevent adversarial content in
    meta.yaml (e.g. user.name) from injecting fake system instructions into
    the briefing f-string output.
    """
    return str(v).replace("\n", " ").replace("\r", "").strip()


_GET_BRIEFING_BASE_DESCRIPTION = """Get a contextual briefing based on meta.yaml and knowledge files.

        Use this at the start of a session to load relevant context.

        Args:
            scope: Optional — 'work', 'school', 'homelab'. If omitted, returns meta + overview.
        """


def _build_briefing_description(briefing_trigger: str) -> str:
    """Prepend read-discipline / wake-word rules to the get_briefing description.

    Same pattern as `_build_knowledge_update_description` in knowledge.py:
    the prepended text is part of the tool schema, so every MCP client
    (including claude.ai web) MUST pass it to the model verbatim.
    """
    if not briefing_trigger:
        return _GET_BRIEFING_BASE_DESCRIPTION
    return f"{briefing_trigger.strip()}\n\n---\n\n{_GET_BRIEFING_BASE_DESCRIPTION}"


def register_briefing_tools(
    mcp: FastMCP, knowledge_dir: Path, *, briefing_trigger: str = ""
):
    briefing_description = _build_briefing_description(briefing_trigger)

    @mcp.tool(description=briefing_description)
    def get_briefing(scope: str | None = None) -> str:
        if scope is not None:
            try:
                require(f"briefing:{scope}")
            except PermissionDenied as e:
                return str(e)

        meta_path = knowledge_dir / "meta.yaml"
        if not meta_path.exists():
            return "No meta.yaml found. Create one in the knowledge directory."

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        parts: list[str] = []

        # Always include core identity (preamble is shared across all tokens)
        user = meta.get("user", {})
        parts.append(f"# Briefing for {_sanitize_meta_value(user.get('name', 'User'))}")
        parts.append(f"Timezone: {_sanitize_meta_value(user.get('timezone', 'unknown'))}")
        parts.append("")

        # Preferences
        prefs = meta.get("preferences", {})
        if prefs:
            pref_lines = [f"- {k}: {_sanitize_meta_value(v)}" for k, v in prefs.items()]
            parts.append("## Preferences")
            parts.extend(pref_lines)
            parts.append("")

        # Scope-specific or all (filtered by token's briefing:* permissions)
        all_scopes = list(meta.get("projects", {}).keys())
        if scope is not None:
            scopes = [scope]
        else:
            allowed = allowed_subscopes("briefing")
            scopes = [s for s in all_scopes if allowed is ALL or s in allowed]

        for s in scopes:
            proj_meta = meta.get("projects", {}).get(s, {})
            if proj_meta:
                parts.append(f"## {s}")
                for k, v in proj_meta.items():
                    parts.append(f"- {k}: {_sanitize_meta_value(v)}")
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
