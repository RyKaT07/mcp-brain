"""
Brain wake tool — dedicated entry point for wake-word activation.

When an LLM sees a user message containing the configured wake word
(default: "jarvis"), this tool should be called FIRST to load full
context: user briefing, active policies, and behavioral rules.

The wake word is read from `meta.yaml` at server startup and baked
into the tool description. Changing it requires a container restart
(tool descriptions are static in FastMCP — set once at registration).
"""

from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_brain.tools._perms import ALL, allowed_subscopes


def _load_wake_word(knowledge_dir: Path) -> str:
    """Read ``preferences.wake_word`` from meta.yaml, default ``jarvis``."""
    meta_path = knowledge_dir / "meta.yaml"
    if not meta_path.exists():
        return "jarvis"
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        return (meta or {}).get("preferences", {}).get("wake_word", "jarvis")
    except Exception:
        return "jarvis"


def _load_policy_summary(knowledge_dir: Path) -> str:
    """Load ``_meta/write-policy.md`` in full (returned inside brain_wake output)."""
    policy_path = knowledge_dir / "_meta" / "write-policy.md"
    if not policy_path.exists():
        return ""
    try:
        return policy_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def register_wake_tools(
    mcp: FastMCP,
    knowledge_dir: Path,
    *,
    briefing_trigger: str = "",
) -> None:
    """Register the ``brain_wake`` tool on *mcp*.

    Must be called **after** all other ``register_*_tools`` so the tool
    inventory snapshot (captured at registration time) is complete.
    """
    wake_word = _load_wake_word(knowledge_dir)

    description = (
        f"Call this FIRST whenever the user says '{wake_word}' or asks "
        f"about their notes, tasks, calendar, or memory. Returns full "
        f"context: user briefing, active policies, and available tools.\n"
        f"\n"
        f"This is the primary entry point for brain activation. Always "
        f"call this before any other brain tool when the wake word is "
        f"detected in the user's message."
    )
    if briefing_trigger:
        description = f"{briefing_trigger.strip()}\n\n---\n\n{description}"

    # Snapshot registered tools at import time (all other tools are
    # already registered because wake tools are registered last).
    tool_lines: list[str] = []
    try:
        for t in sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name):
            if t.name == "brain_wake":
                continue
            first_line = (t.description or "").strip().split("\n")[0][:100]
            tool_lines.append(f"- **{t.name}**: {first_line}")
    except Exception:
        pass

    @mcp.tool(description=description)
    def brain_wake() -> str:
        """Activate brain context. Returns briefing, policy, and tool inventory."""
        meta_path = knowledge_dir / "meta.yaml"
        if not meta_path.exists():
            return "No meta.yaml found. Create one in the knowledge directory."

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        parts: list[str] = []

        # ── User identity ──────────────────────────────────────────
        user = meta.get("user", {})
        parts.append(f"# Brain activated — {user.get('name', 'User')}")
        parts.append(f"Timezone: {user.get('timezone', 'unknown')}")
        parts.append("")

        # ── Preferences ────────────────────────────────────────────
        prefs = meta.get("preferences", {})
        if prefs:
            parts.append("## Preferences")
            for k, v in prefs.items():
                parts.append(f"- {k}: {v}")
            parts.append("")

        # ── Projects (filtered by token permissions) ───────────────
        all_scopes = list(meta.get("projects", {}).keys())
        allowed = allowed_subscopes("briefing")
        scopes = [s for s in all_scopes if allowed is ALL or s in allowed]

        for s in scopes:
            proj_meta = meta.get("projects", {}).get(s, {})
            if proj_meta:
                parts.append(f"## {s}")
                for k, v in proj_meta.items():
                    parts.append(f"- {k}: {v}")
                parts.append("")

            scope_dir = knowledge_dir / s
            if scope_dir.exists():
                files = sorted(scope_dir.glob("*.md"))
                if files:
                    parts.append(f"### Available knowledge files ({s}/)")
                    for f in files:
                        parts.append(f"- {f.stem}")
                    parts.append("")

        # ── Active policy rules ────────────────────────────────────
        policy = _load_policy_summary(knowledge_dir)
        if policy:
            parts.append("## Active policy rules")
            parts.append("")
            parts.append(policy)
            parts.append("")

        # ── Available tools ────────────────────────────────────────
        if tool_lines:
            parts.append("## Available tools")
            parts.extend(tool_lines)
            parts.append("")

        return "\n".join(parts)
